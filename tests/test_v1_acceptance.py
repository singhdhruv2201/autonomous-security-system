"""Phase 7: isolated real services/Neo4j, scripted investigation, fake Slack transport."""
import asyncio
from contextlib import ExitStack
import secrets
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from fastapi.responses import JSONResponse
from langgraph.checkpoint.sqlite import SqliteSaver
from helio_simulator.api import create_app as directory_app
from helio_crm.api import create_app as crm_app
from helio_workbench.api import create_app as workbench_app
from helio_workbench.store import WorkbenchStore
from outside.api import create_app as outside_app
from helio_security.api import create_app as security_app
from helio_dashboard.api import create_app as dashboard_app
from helio_dashboard.gateway import Gateway
from helio_security.settings import Settings
from helio_security.platform_store import PlatformStore
from helio_security.reader import GraphReader
from helio_security.worker import ObservationWorker
from helio_security.agent_worker import AgentWorker
from helio_security.workflow import build_graph
from helio_security.mcp_runtime import mcp_connections
from helio_security.mcp_actions import create_mcp_playbooks
from helio_security.slack_app import SlackActions, SlackConfig, SlackNotifier
from test_graph import graph, serve
from test_workflow import make_deps
from test_slack_app import FakeClient, click

pytestmark=pytest.mark.neo4j

@pytest.fixture
def demo(graph,directory,tmp_path,monkeypatch):
    tokens={k:secrets.token_urlsafe(32) for k in ('admin','maya','agent','observer','actions','renewal_tracker','support_lookup','lead_enrichment','renewal_digest')}
    fault={'ambiguous':False,'patches':0}
    crm=crm_app(tmp_path/'crm.db',tokens,directory.employees)
    @crm.middleware('http')
    async def lost_ack(request,call_next):
        if fault['ambiguous'] and request.method=='PATCH':
            fault['patches']+=1
            await call_next(request)
            return JSONResponse({},status_code=503)
        if fault['ambiguous'] and fault['patches'] and request.url.path=='/accounts':
            return JSONResponse({},status_code=503)
        return await call_next(request)
    with ExitStack() as stack:
        durl=stack.enter_context(serve(directory_app(directory.path)))
        curl=stack.enter_context(serve(crm))
        ourl=stack.enter_context(serve(outside_app(tmp_path/'outside.db')))
        wb=WorkbenchStore(tmp_path/'wb.db')
        wurl=stack.enter_context(serve(workbench_app(store=wb,crm_url=curl,observer_token=tokens['observer'],action_token=tokens['actions'],user_token=tokens['maya'],crm_tokens={
            'service:renewal-tracker':tokens['renewal_tracker'],'service:support-lookup':tokens['support_lookup'],
            'agent:lead-enrichment':tokens['lead_enrichment'],'agent:renewal-digest':tokens['renewal_digest']})))
        deps=make_deps(tmp_path);deps.checkpointer=stack.enter_context(SqliteSaver.from_conn_string(str(tmp_path/'checkpoints.db')))
        platform=PlatformStore(deps.store.path);platform.initialize();deps.platform_store=platform
        # Same local Helio domain, isolated Neo4j namespace owned by the graph fixture.
        class Scoped:
            company_id='helio'
            def __getattr__(self,name): return getattr(graph,name)
        scoped=Scoped();reader=GraphReader(graph)
        observer=ObservationWorker(platform,scoped,reader,durl,curl,tokens['observer'],workbench_url=wurl,workbench_token=tokens['observer'],interval=0)
        settings=Settings(operator_token=secrets.token_urlsafe(32),viewer_token=secrets.token_urlsafe(32),observer_token=tokens['observer'],db_path=deps.store.path,sync_interval=0)
        api=stack.enter_context(serve(security_app(settings,graph=scoped,reader=reader,store=platform,start_worker=False)))
        dashboard=stack.enter_context(serve(dashboard_app(Gateway(api,settings.viewer_token,settings.operator_token))))
        for key,value in {'CRM_URL':curl,'CRM_ACTION_TOKEN':tokens['actions'],'CRM_OBSERVER_TOKEN':tokens['observer'],
            'WORKBENCH_URL':wurl,'WORKBENCH_OBSERVER_TOKEN':tokens['observer'],'WORKBENCH_ACTION_TOKEN':tokens['actions'],
            'SECURITY_API_URL':api,'SECURITY_VIEWER_TOKEN':settings.viewer_token,'AWASI_APPROVED_DESTINATIONS':'partner-api,127.0.0.1'}.items(): monkeypatch.setenv(key,value)
        config=SlackConfig(channel='C123',default_user='U123');client=FakeClient();notifier=SlackNotifier(deps.store,client,config);deps.notifier=notifier
        handler=SlackActions(deps.store,notifier,config,team_id='T123')
        def restart():
            deps.checkpointer.conn.close()
            deps.checkpointer=stack.enter_context(SqliteSaver.from_conn_string(str(tmp_path/'checkpoints.db')))
            return AgentWorker(platform,deps.store,deps,graph=build_graph(deps),company_id='helio')
        def observe():
            platform.enqueue('helio','acceptance','manual',time.time());assert observer.run_once()
        def request(method,path,body=None):
            response=httpx.request(method,wurl+path,headers={'Authorization':'Bearer '+tokens['actions' if method=='PATCH' else 'maya']},json=body)
            assert response.status_code==200,response.text
            return response.json()
        def trigger(scenario):
            if scenario=='orphan': directory.set_employee_status('emp-maya-chen','inactive')
            elif scenario=='public': request('PATCH','/apps/app-renewal-tracker',{'expected_version':wb.get_app_by_id('app-renewal-tracker')['version'],'visibility':'public'})
            else:
                request('POST','/agents/agent-lead-enrichment/destinations',{'host':'localhost','url':ourl.replace('127.0.0.1','localhost')+'/collect'})
                request('POST','/agents/agent-lead-enrichment/run')
            observe()
        async def answer(worker,cid,choice,user='U123'):
            question=deps.store.get_case_full(cid)['question'];key=question['key']
            ts=notifier.slack_store.message(cid,key)['ts']
            await handler.handle(ack=AsyncMock(),body=click(cid,choice=choice,key=key,ts=ts,user=user))
            await worker.run_once();await notifier.flush()
        yield SimpleNamespace(**locals())


@pytest.mark.parametrize('scenario',['orphan','public','external'])
def test_three_scenarios_through_real_services_and_dashboard(demo,scenario):
    d=demo
    async def run():
        async with mcp_connections() as (_,actions):
            d.deps.playbooks=create_mcp_playbooks(actions)
            worker=AgentWorker(d.platform,d.deps.store,d.deps,company_id='helio')
            d.observe();await worker.run_once()
            assert not d.platform.findings('helio')['items'] and not d.client.posts
            d.trigger(scenario);await worker.run_once();await d.notifier.flush()
            case=d.deps.store.active_cases('helio')[0];cid=case['id']
            # A valid case button from an unmapped Slack identity cannot progress it.
            before=d.deps.store.get_case_full(cid)
            await d.answer(worker,cid,before['question']['choices'][0],user='U_UNMAPPED')
            assert d.deps.store.get_case_full(cid)['actions']==before['actions']
            assert d.deps.store.get_case(cid)['status']==case['status']
            # Recreate the graph/worker against durable checkpoints while waiting.
            worker=d.restart()
            choices={'orphan':['retire','approve'],'public':['restrict','keep_restricted'],'external':['remove_connection']}[scenario]
            for choice in choices: await d.answer(worker,cid,choice)
            assert d.deps.store.get_case(cid)['status']=='resolved'
            assert all(a['verified_at'] for a in d.deps.store.get_actions(cid))
            detail=httpx.get(d.dashboard+'/api/cases/'+cid).json()
            assert detail==d.deps.store.get_case_full(cid)
            assert detail['events'] and d.client.posts
            d.observe();assert not d.platform.findings('helio',status='open')['items']
            if scenario=='orphan':
                assert httpx.get(d.curl+'/customers',headers={'Authorization':'Bearer '+d.tokens['agent']}).status_code==403
            if scenario=='public': assert httpx.get(d.wurl+'/public/renewals').status_code==404
            if scenario=='external':
                assert httpx.get(d.ourl+'/received').json()['total_records']==6
                assert d.wb.get_agent('agent-lead-enrichment')['status']=='active'
                assert not d.wb.list_destinations('agent-lead-enrichment')[0]['enabled']
                d.request('POST','/agents/agent-lead-enrichment/run')
                assert httpx.get(d.ourl+'/received').json()['total_records']==6
            assert all(token not in str(detail)+str(d.client.posts) for token in d.tokens.values())
            assert 'contact@northstar.example' not in str(detail)+str(d.client.posts)
    asyncio.run(run())


def test_undo_restores_then_requires_approver(demo):
    d=demo
    async def run():
        async with mcp_connections() as (_,actions):
            d.deps.playbooks=create_mcp_playbooks(actions);worker=AgentWorker(d.platform,d.deps.store,d.deps,company_id='helio')
            d.trigger('public');await worker.run_once();await d.notifier.flush()
            cid=d.deps.store.active_cases('helio')[0]['id']
            await d.answer(worker,cid,'restrict');await d.answer(worker,cid,'undo')
            assert d.wb.get_app_by_id('app-renewal-tracker')['visibility']=='public'
            assert d.deps.store.get_case(cid)['status']=='needs_review'
            assert d.deps.store.has_human_override('helio',d.deps.store.get_case(cid)['finding_id'])
            d.observe();await worker.run_once();await d.notifier.flush()
            next_case=next(c for c in d.deps.store.active_cases('helio') if c['id']!=cid)
            assert d.deps.store.get_case_full(next_case['id'])['question']['role']=='approver'
            assert not d.deps.store.get_actions(next_case['id'])
    asyncio.run(run())


def test_ambiguous_action_requires_review(demo):
    d=demo
    async def run():
        async with mcp_connections() as (_,actions):
            d.deps.playbooks=create_mcp_playbooks(actions);worker=AgentWorker(d.platform,d.deps.store,d.deps,company_id='helio')
            d.trigger('orphan');d.fault['ambiguous']=True
            await worker.run_once()
            case=d.deps.store.list_cases('helio')['items'][0]
            assert case['status']=='needs_review'
            assert d.deps.store.get_actions(case['id'])[0]['status']=='unknown'
            assert d.fault['patches']==1
    asyncio.run(run())


def test_all_three_controls_stay_silent_including_actual_approved_send(demo):
    d=demo
    destination=d.wb.list_destinations('agent-renewal-digest')[0]
    d.request('PATCH','/agents/agent-renewal-digest/destinations/partner-api',{'expected_version':destination['version'],'enabled':False})
    d.request('POST','/agents/agent-renewal-digest/destinations',{'host':'127.0.0.1','url':d.ourl+'/collect'})
    d.request('POST','/agents/agent-renewal-digest/run')
    assert httpx.get(d.ourl+'/received').json()['total_records']==6
    assert httpx.get(d.wurl+'/public/support').status_code==404
    d.observe()
    asyncio.run(AgentWorker(d.platform,d.deps.store,d.deps,company_id='helio').run_once())
    assert not d.platform.findings('helio')['items']
    assert not d.deps.store.list_cases('helio')['items']
    assert not d.client.posts
