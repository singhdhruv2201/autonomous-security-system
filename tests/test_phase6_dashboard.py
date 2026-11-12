"""Real dashboard HTTP -> security API -> durable workflow records."""
import asyncio
import secrets
from unittest.mock import Mock
import httpx
import pytest
from helio_dashboard.api import create_app as dashboard_app
from helio_dashboard.gateway import Gateway
from helio_security.api import create_app as security_app
from helio_security.settings import Settings
from helio_security.platform_store import PlatformStore
from test_graph import serve
from test_phase5_workflow import setup,answer,APP_RULE,AGENT_RULE
from test_playbooks_workbench import workbench
from test_case_crm_integration import environment


def check_http(deps,cid):
    settings=Settings(operator_token=secrets.token_urlsafe(32),viewer_token=secrets.token_urlsafe(32),
        observer_token=secrets.token_urlsafe(32),db_path=deps.store.path,sync_interval=0)
    graph=Mock(company_id='helio');graph.status.return_value={'company_id':'helio','counts':{},
        'coverage_complete':True,'sources':{s:{'fresh':True} for s in ('directory','crm','workbench')}}
    reader=Mock();reader.entities.return_value={'items':[],'total':0};reader.graph.return_value={'nodes':[],'edges':[]}
    platform=deps.platform_store or PlatformStore(deps.store.path)
    with serve(security_app(settings,graph=graph,reader=reader,store=platform,start_worker=False)) as api_url, serve(
        dashboard_app(Gateway(api_url,settings.viewer_token,settings.operator_token))) as dashboard_url:
        state=httpx.get(dashboard_url+'/api/state').json()
        assert state['errors']=={}
        assert any(c['id']==cid for c in state['cases']['items'])
        detail=httpx.get(dashboard_url+'/api/cases/'+cid).json()
        expected=deps.store.get_case_full(cid)
        assert detail==expected
        assert detail['events'] and detail['actions'] and detail['spans']
        assert all(a['verified_at'] is not None for a in detail['actions'])
        assert len(state['policies']['decision_rules'])==6
        assert httpx.post(dashboard_url+'/api/cases/'+cid+'/decisions',json={'choice':'undo'}).status_code==404
        assert settings.viewer_token not in str(detail)+str(state)
        assert 'Northstar' not in str(detail)


@pytest.mark.parametrize('rule',[APP_RULE,AGENT_RULE])
def test_workbench_case_timeline_uses_actual_workflow_records(tmp_path,workbench,rule):
    deps,g,cid,cfg,r=setup(tmp_path,workbench,rule,leaking=True)
    answer(g,cfg,r,'keep_restricted' if rule==APP_RULE else 'remove_connection')
    assert deps.store.get_case(cid)['status']=='resolved'
    check_http(deps,cid)


def test_inactive_owner_case_timeline_uses_actual_workflow_records(environment):
    deps,g,cid,cfg,state,*_=environment
    asyncio.run(g.ainvoke(state,cfg))
    check_http(deps,cid)
