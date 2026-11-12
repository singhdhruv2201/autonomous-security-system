import asyncio
from concurrent.futures import ThreadPoolExecutor
import json
from threading import Barrier, BrokenBarrierError
from types import SimpleNamespace
import time
from unittest.mock import patch

import httpx
import pytest
from langchain_core.messages import AIMessage
from helio_security.actions import dispatch
from helio_security.cases import CaseStore
from helio_security.investigator import LimitedChatModel, extract_evidence_ids, validate_and_sanitize_assessment
from helio_security.live_ledger import LiveLedger, BudgetExceededError
from helio_security.mcp_read import get_crm_activity, _fetch_bounded, MAX_RESPONSE_BYTES
from helio_security.mcp_actions import create_mcp_playbooks
from helio_security.tracing import filter_attributes

def test_activity_uses_existing_api_route_and_filter(tmp_path, monkeypatch):
    from helio_security.api import create_app
    from helio_security.settings import Settings
    from test_graph import serve
    class Graph:
        company_id = 'helio'
        def initialize(self): pass
    class Activity:
        query = None
        def get(self, path, company, query):
            self.query = query
            return {'company_id': company, 'items': [{'id': 'event-1', 'actor_id': query['actor_id']}]}
    activity = Activity()
    settings = Settings(operator_token='o'*32, viewer_token='v'*32, observer_token='b'*32,
                        db_path=tmp_path/'security.sqlite3', company_id='helio', sync_interval=0)
    app = create_app(settings, graph=Graph(), start_worker=False, activity_client=activity)
    with serve(app) as url:
        monkeypatch.setenv('SECURITY_API_URL', url)
        monkeypatch.setenv('SECURITY_VIEWER_TOKEN', 'v'*32)
        result = asyncio.run(get_crm_activity('agent:sales-research', limit=3))
    assert result.get('items') == [{'id':'event-1', 'actor_id':'agent:sales-research'}]
    assert activity.query['actor_id'] == 'agent:sales-research'
    assert activity.query['limit'] == 3

def test_two_reservers_cannot_both_take_last_slot(tmp_path):
    path=tmp_path/'ledger.json'
    LiveLedger(path,max_requests=1)
    barrier=Barrier(2)
    class SynchronizedLedger(LiveLedger):
        def get_data(self):
            data=super().get_data()
            try:
                barrier.wait(timeout=0.2)
            except BrokenBarrierError:
                pass  # A correct transaction lock serializes these reads.
            return data
    def reserve():
        try:
            return SynchronizedLedger(path,max_requests=1).check_and_reserve()
        except BudgetExceededError:
            return None
    with ThreadPoolExecutor(max_workers=2) as pool:
        results=list(pool.map(lambda _:reserve(),range(2)))
    assert len([r for r in results if r])==1

def test_reservation_matches_bounded_actual_request(tmp_path):
    async def run():
        class Model:
            max_tokens=2048
            calls=0
            async def ainvoke(self,*args,**kwargs):
                self.calls+=1
                return AIMessage(content='ok',usage_metadata={'input_tokens':12000,'output_tokens':2048,'total_tokens':14048})
        ledger=LiveLedger(tmp_path/'ledger.json',max_spend_usd=0.001)
        model=Model()
        wrapped=LimitedChatModel(model,ledger=ledger,model_name='claude-haiku-5-5')
        try:
            await wrapped.ainvoke('large context '*12000)
        except BudgetExceededError:
            pass
        assert model.calls==0,ledger.get_data()['total_estimated_cost_usd']
    asyncio.run(run())

def test_timeout_does_not_invent_zero_output_usage(tmp_path):
    async def run():
        class Model:
            async def ainvoke(self,*args,**kwargs):
                raise TimeoutError('response lost')
        ledger=LiveLedger(tmp_path/'ledger.json')
        wrapped=LimitedChatModel(Model(),ledger=ledger)
        with pytest.raises(TimeoutError):
            await wrapped.ainvoke('context')
        data=ledger.get_data()
        assert data['pending_reservations'] or data['runs'][0]['output_tokens'] is None
    asyncio.run(run())

def test_byte_ceiling_stops_reading_oversized_body():
    async def run():
        class Stream(httpx.AsyncByteStream):
            chunks=0
            async def __aiter__(self):
                for _ in range(10):
                    self.chunks+=1
                    yield b'x'*MAX_RESPONSE_BYTES
        stream=Stream()
        async def handle(request):
            return httpx.Response(200,stream=stream)
        async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
            result=await _fetch_bounded(client,'http://review.local/test',{})
        assert result.get('error')=='response_too_large'
        assert stream.chunks<=2
    asyncio.run(run())

def test_failed_tool_response_does_not_count_as_observed_entity():
    result={'found':False,'entity_id':'agent:missing','error':'not_found'}
    assert extract_evidence_ids(result)==set()

def test_assessment_values_are_sanitized_before_persistence():
    raw={'summary':'Customer SSN 123-45-6789 and API key sk-review-FAKESECRET', 'impact':'Customer annual_revenue 4200000', 'likely_cause':'unknown', 'confidence':'low'}
    out=validate_and_sanitize_assessment(raw,set(),'inactive_owner_agent_access')
    assert 'FAKESECRET' not in json.dumps(out)
    assert '123-45-6789' not in json.dumps(out)

def test_trace_argument_values_cannot_store_secrets():
    out=filter_attributes({'entity_id':'Bearer REVIEW_FAKE_SECRET'})
    assert 'REVIEW_FAKE_SECRET' not in json.dumps(out)

def test_mcp_lost_mutation_response_is_reconciled(tmp_path):
    store=CaseStore(tmp_path/'case.sqlite3');store.initialize()
    cid=store.open_case('helio','finding-1',route='fixing',now=time.time())
    class Transport:
        changed=False
        async def call_tool(self,name,args):
            if name=='crm_read':
                return {'principal_status':'active','access_version':1}
            if name=='crm_pause_principal':
                self.changed=True
                raise TimeoutError('lost MCP response after commit')
            if name=='crm_verify':
                return self.changed
    transport=Transport()
    deps=SimpleNamespace(store=store,clock=time.time,playbooks=create_mcp_playbooks(transport),crm_url='',action_token='',observer_token='')
    result=asyncio.run(dispatch(deps,cid,'crm.pause_principal','agent:sales-research'))
    assert result['status']=='executed',result['result']


def test_managed_stdio_runs_real_crm_and_security_api(tmp_path, monkeypatch):
    from helio_security.mcp_runtime import mcp_connections
    from helio_security.api import create_app as security_app
    from helio_security.settings import Settings
    from helio_crm.api import create_app as crm_app
    from test_graph import serve
    from test_playbooks_crm import make_tokens, EMPLOYEES
    class Graph:
        company_id = 'helio'
        def initialize(self): pass
    class Activity:
        def get(self, path, company, query):
            return {'company_id':company, 'items':[{'id':'event-1','actor_id':query['actor_id']}]}
    tokens=make_tokens()
    settings=Settings(operator_token='o'*32, viewer_token='v'*32, observer_token='b'*32,
                      db_path=tmp_path/'security.sqlite3', company_id='helio',sync_interval=0)
    api=security_app(settings,graph=Graph(),start_worker=False,activity_client=Activity())
    with serve(api) as api_url, serve(crm_app(tmp_path/'crm.sqlite3',tokens,lambda:EMPLOYEES)) as crm_url:
        for key,value in {'SECURITY_API_URL':api_url,'SECURITY_VIEWER_TOKEN':'v'*32,
                          'CRM_URL':crm_url,'CRM_ACTION_TOKEN':tokens['actions'],
                          'CRM_OBSERVER_TOKEN':tokens['observer']}.items():
            monkeypatch.setenv(key,value)
        async def run():
            async with mcp_connections() as (read, actions):
                assert read.client.is_connected() and actions.client.is_connected()
                tools=await read.list_tools()
                assert not any('pause' in t.name for t in tools)
                tool=next(t for t in tools if t.name=='get_crm_activity')
                result=await tool.ainvoke({'actor_id':'agent:sales-research','limit':3})
                assert 'event-1' in extract_evidence_ids(result)
                pb=create_mcp_playbooks(actions)['crm.pause_principal']
                before=await pb.read('agent:sales-research')
                assert await pb.verify('agent:sales-research') is False
                await pb.run('agent:sales-research',before_state=before)
                assert await pb.verify('agent:sales-research') is True
                await pb.undo('agent:sales-research',before=before)
                assert await pb.verify('agent:sales-research',expected_state='active') is True
            assert not read.client.is_connected() and not actions.client.is_connected()
        asyncio.run(run())


def test_ledger_settlement_is_idempotent_and_keeps_other_reservations(tmp_path):
    ledger=LiveLedger(tmp_path/'ledger.json')
    one=ledger.check_and_reserve()
    two=ledger.check_and_reserve()
    ledger.record_request('claude-haiku-5-5',10,5,reservation_id=one)
    ledger.record_request('claude-haiku-5-5',10,5,reservation_id=one)
    data=ledger.get_data()
    assert data['total_requests']==1
    assert set(data['pending_reservations'])=={two}


def test_missing_model_usage_stays_unknown(tmp_path):
    class Model:
        async def ainvoke(self,*args,**kwargs): return AIMessage(content='ok')
    ledger=LiveLedger(tmp_path/'ledger.json')
    asyncio.run(LimitedChatModel(Model(),ledger=ledger).ainvoke('test'))
    data=ledger.get_data()
    assert data['runs'][0]['input_tokens'] is None
    assert data['runs'][0]['output_tokens'] is None
    assert data['total_estimated_cost_usd']>0


def test_ledger_uses_long_context_price_for_long_request():
    from helio_security.live_ledger import calculate_cost
    assert calculate_cost(1_000_000,1_000_000)==3.0


def test_legacy_accounting_is_preserved_but_blocks_new_calls(tmp_path):
    path=tmp_path/'ledger.json'
    ledger=LiveLedger(path)
    data=ledger.get_data()
    del data['schema_version']
    data['total_requests']=5
    path.write_text(json.dumps(data))
    with pytest.raises(BudgetExceededError, match='reconciliation'):
        LiveLedger(path).check_and_reserve()
    assert json.loads(path.read_text())['total_requests']==5


def test_assessment_storage_is_a_redaction_boundary(tmp_path):
    store=CaseStore(tmp_path/'case.sqlite3'); store.initialize()
    cid=store.open_case('helio','finding-1',route='investigating',now=time.time())
    store.save_investigation(cid, {'summary':'Credential sk-review-PRIVATE, SSN 123-45-6789',
                                  'impact':'email private@example.test',
                                  'raw_response':{'secret':'DO_NOT_STORE'}})
    saved=store.get_investigation(cid)
    assert 'PRIVATE' not in json.dumps(saved)
    assert '123-45-6789' not in json.dumps(saved)
    assert 'DO_NOT_STORE' not in json.dumps(saved)


def test_read_tool_excludes_customer_values_in_unknown_fields():
    from test_mcp_read import mock_http
    from helio_security.mcp_read import get_finding
    payload={'id':'finding-1', 'evidence':{'record_count':6, 'company_id':'helio',
            'customer':{'name':'CUSTOMER_VALUE', 'annual_revenue':99999},
            'notes':'Private customer value CUSTOMER_VALUE'}}
    with mock_http(httpx.Response(200,json=payload)):
        result=asyncio.run(get_finding('finding-1'))
    assert 'CUSTOMER_VALUE' not in json.dumps(result)
    assert result['evidence']['record_count']==6


def test_configured_worker_budget_is_attached_before_running(tmp_path, monkeypatch):
    from contextlib import asynccontextmanager
    from helio_security.agent_worker import run_with_mcp
    from helio_security.investigator import ModelInvestigator
    @asynccontextmanager
    async def connections():
        class Read:
            async def list_tools(self): return []
        yield Read(), SimpleNamespace()
    monkeypatch.setattr('helio_security.mcp_runtime.mcp_connections',connections)
    monkeypatch.setenv('AWASI_LIVE_LEDGER',str(tmp_path/'ledger.json'))
    class Worker:
        deps=SimpleNamespace(investigator=ModelInvestigator(tools=[]),playbooks={})
        async def run_forever(self):
            assert isinstance(self.deps.investigator.ledger,LiveLedger)
            assert self.deps.investigator.ledger.path==tmp_path/'ledger.json'
    asyncio.run(run_with_mcp(Worker()))


def test_mcp_start_failure_closes_slack_handler(monkeypatch):
    from contextlib import asynccontextmanager
    from unittest.mock import AsyncMock
    from helio_security.agent_worker import run_with_mcp
    monkeypatch.delenv('AWASI_LIVE_LEDGER', raising=False)
    @asynccontextmanager
    async def connections():
        raise RuntimeError('startup failed')
        yield
    monkeypatch.setattr('helio_security.mcp_runtime.mcp_connections', connections)
    handler = SimpleNamespace(close_async=AsyncMock())
    with pytest.raises(RuntimeError, match='startup failed'):
        asyncio.run(run_with_mcp(SimpleNamespace(), handler))
    handler.close_async.assert_awaited_once()


def test_activity_projection_preserves_availability_and_response_checks():
    from helio_security.mcp_read import project_evidence
    disabled = {'enabled': False, 'items': [], 'total': 0}
    assert project_evidence(disabled) == disabled
    payload = {'enabled': True, 'items': [{
        'completion': 'interrupted', 'scan_status': 'not_run',
        'release_decision': 'allowed', 'serialized_bytes': 50, 'emitted_bytes': 20,
    }]}
    assert project_evidence(payload) == payload


def test_worker_cancellation_closes_both_mcp_sessions(monkeypatch):
    from helio_security import mcp_runtime
    from helio_security.agent_worker import run_with_mcp
    from helio_security.investigator import ModelInvestigator
    monkeypatch.delenv('AWASI_LIVE_LEDGER',raising=False)
    adapters=[]
    original=mcp_runtime.MCPAdapter
    def capture(client):
        adapter=original(client)
        adapters.append(adapter)
        return adapter
    monkeypatch.setattr(mcp_runtime,'MCPAdapter',capture)
    async def run():
        started=asyncio.Event()
        class Worker:
            deps=SimpleNamespace(investigator=ModelInvestigator(tools=[]),playbooks={})
            async def run_forever(self):
                assert len(self.deps.investigator._tools)==6
                assert set(self.deps.playbooks)=={'crm.pause_principal','crm.revoke_grant','workbench.restrict_app','workbench.pause_agent','workbench.disable_destination'}
                started.set()
                await asyncio.Event().wait()
        task=asyncio.create_task(run_with_mcp(Worker()))
        try:
            await asyncio.wait_for(started.wait(),10)
        finally:
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(task,10)
        assert len(adapters)==2
        assert not any(adapter.client.is_connected() for adapter in adapters)
    asyncio.run(run())


def test_second_mcp_start_failure_closes_first_session(monkeypatch):
    from helio_security import mcp_runtime
    original=mcp_runtime.MCPAdapter
    adapters=[]
    def capture(client):
        if adapters:
            raise RuntimeError('simulated startup failure')
        adapter=original(client)
        adapters.append(adapter)
        return adapter
    monkeypatch.setattr(mcp_runtime,'MCPAdapter',capture)
    async def run():
        with pytest.raises(RuntimeError,match='startup failure'):
            async with mcp_runtime.mcp_connections():
                pytest.fail('startup should not complete')
        assert not adapters[0].client.is_connected()
    asyncio.run(run())


from test_case_crm_integration import environment


@pytest.mark.parametrize('mode,expected', [('before_commit','unknown'),('after_commit','executed'),('unreadable','unknown')])
def test_real_stdio_workflow_preserves_lost_response_recovery(environment, monkeypatch, mode, expected):
    from helio_security.mcp_runtime import mcp_connections
    deps,graph,cid,config,state,fault=environment
    for key,value in {'CRM_URL':deps.crm_url,'CRM_ACTION_TOKEN':deps.action_token,
                      'CRM_OBSERVER_TOKEN':deps.observer_token}.items():
        monkeypatch.setenv(key,value)
    fault['mode']=mode
    async def run():
        async with mcp_connections() as (_,actions):
            deps.playbooks=create_mcp_playbooks(actions)
            await graph.ainvoke(state,config)
            await graph.ainvoke(None,config)
    asyncio.run(run())
    assert fault['patches']==1
    action=deps.store.get_actions(cid)[0]
    assert action['status']==expected
    assert action['before']['principal_status']=='active'
    if expected=='unknown':
        assert deps.store.get_case(cid)['status']=='needs_review'
