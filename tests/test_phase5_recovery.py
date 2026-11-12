"""Fault injection across real workbench HTTP and durable action/checkpoint storage."""
import asyncio
import pytest
from fastapi.responses import JSONResponse
from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.types import Command
from helio_workbench.store import WorkbenchStore
from helio_workbench.api import create_app
from helio_security.workflow import build_graph
from helio_security.playbooks import REGISTRY
from test_graph import serve
from test_workflow import make_deps, make_facts
from test_case_recovery import Crash

@pytest.fixture
def environment(tmp_path):
    wb=WorkbenchStore(tmp_path/'wb.db');wb.update_app('app-renewal-tracker',1,visibility='public')
    app=create_app(store=wb,observer_token='o'*32,action_token='a'*32,user_token='u'*32)
    fault={'mode':None,'patches':0}
    @app.middleware('http')
    async def inject(request,call_next):
        if request.method=='PATCH':
            fault['patches']+=1
            if fault['mode']=='before_commit': return JSONResponse({},503)
            response=await call_next(request)
            if fault['mode'] in ('after_commit','unreadable'): return JSONResponse({},503)
            return response
        if fault['mode']=='unreadable' and fault['patches'] and request.url.path=='/inventory':
            return JSONResponse({},503)
        return await call_next(request)
    with serve(app) as url:
        deps=make_deps(tmp_path)
        deps.workbench_url=url;deps.workbench_observer_token='o'*32;deps.workbench_action_token='a'*32
        deps.playbooks=REGISTRY
        with SqliteSaver.from_conn_string(str(tmp_path/'checkpoint.db')) as saver:
            deps.checkpointer=saver
            cid=deps.store.open_case('helio','public-finding',route='investigating',now=deps.clock())
            state={'finding_id':'public-finding','company_id':'helio','facts':make_facts(
                rule='public_app_confidential_data',owner_status='active',target_kind='app',exposure='public',
                leaking_now=True,app_id='app-renewal-tracker',addressed_to='emp-luca-rossi')}
            yield deps,build_graph(deps),cid,{'configurable':{'thread_id':cid}},state,fault,wb

@pytest.mark.parametrize('mode,expected',[('before_commit','unknown'),('after_commit','executed'),('unreadable','unknown')])
def test_lost_workbench_response_never_repeats_mutation(environment,mode,expected):
    deps,g,cid,cfg,state,fault,wb=environment
    fault['mode']=mode
    asyncio.run(g.ainvoke(state,cfg))
    asyncio.run(build_graph(deps).ainvoke(None,cfg))
    assert fault['patches']==1
    a=deps.store.get_actions(cid)[0]
    assert a['status']==expected and a['before']['visibility']=='public'
    if expected=='unknown': assert deps.store.get_case(cid)['status']=='needs_review'

@pytest.mark.parametrize('undo',[False,True])
def test_crash_after_commit_recovers_saved_state_without_repeating_patch(environment,monkeypatch,undo):
    deps,g,cid,cfg,state,fault,wb=environment
    if undo: asyncio.run(g.ainvoke(state,cfg))
    original=deps.store.update_action
    def crash(action_id,**kwargs):
        if kwargs.get('status')=='executed': raise Crash()
        return original(action_id,**kwargs)
    monkeypatch.setattr(deps.store,'update_action',crash)
    command=Command(resume={'choice':'undo','employee_id':'operator'}) if undo else state
    with pytest.raises(Crash): asyncio.run(g.ainvoke(command,cfg))
    monkeypatch.setattr(deps.store,'update_action',original)
    asyncio.run(build_graph(deps).ainvoke(None,cfg))
    assert fault['patches']==(2 if undo else 1)
    assert wb.get_app_by_slug('renewals')['visibility']==('public' if undo else 'company')
    if undo: assert deps.store.has_human_override('helio','public-finding')
    assert 'o'*32 not in str(deps.store.get_case_full(cid))
