import asyncio
import pytest
from langgraph.types import Command
from helio_security.workflow import build_graph
from helio_security.playbooks import REGISTRY
from helio_security.platform_store import PlatformStore
from test_workflow import make_deps, make_facts
from test_playbooks_workbench import workbench
from test_phase5_graph import APP_RULE, AGENT_RULE, evaluation, finish


def setup(tmp_path,workbench,rule=APP_RULE,leaking=False,override=False):
    store,app,cfg=workbench
    if rule==AGENT_RULE:
        store.add_destination('agent-lead-enrichment','enrich-leads','http://enrich-leads:8005/collect')
    deps=make_deps(tmp_path)
    for key,value in cfg.items(): setattr(deps,key,value)
    deps.playbooks=REGISTRY
    deps.clock=lambda:1000
    p=PlatformStore(tmp_path/'platform.db');p.initialize()
    ev=evaluation(rule,1000)
    ev['candidates'][0]['identity']=(['agent-lead-enrichment','enrich-leads'] if rule==AGENT_RULE else ['app-renewal-tracker','grant','data'])
    finish(p,ev,1000)
    fid=p.findings('helio',now=1000)['items'][0]['id']
    deps.platform_store=p
    facts=make_facts(rule=rule,owner_status='active',target_kind='app' if rule==APP_RULE else 'agent',
        exposure='public' if rule==APP_RULE else 'external',leaking_now=leaking,
        human_override=override,addressed_to='emp-luca-rossi' if rule==APP_RULE else 'emp-zara-ali',
        app_id='app-renewal-tracker',agent_id='agent-lead-enrichment',host='enrich-leads')
    cid=deps.store.open_case('helio',fid,route='investigating',now=1000)
    config={'configurable':{'thread_id':cid}}
    graph=build_graph(deps)
    result=asyncio.run(graph.ainvoke({'finding_id':fid,'company_id':'helio','facts':facts},config))
    return deps,graph,cid,config,result


def answer(graph,config,result,choice,employee=None):
    q=result['question']
    return asyncio.run(graph.ainvoke(Command(resume={'choice':choice,'employee_id':employee or q['addressed_to'],'key':q['key']}),config))

@pytest.mark.parametrize('choice',['restrict','timeout','intentional'])
def test_public_no_views_owner_paths(tmp_path,workbench,choice):
    deps,g,cid,cfg,r=setup(tmp_path,workbench)
    assert deps.store.get_actions(cid)==[]
    assert r['question']['choices']==['restrict','intentional']
    if choice=='timeout': deps.clock=lambda:2000
    r=answer(g,cfg,r,choice,'system' if choice=='timeout' else None)
    if choice=='intentional':
        assert deps.store.get_case(cid)['status']=='resolved'
        assert deps.platform_store.finding('helio',r['finding_id'],now=1000)['reason']=='exception_active'
        assert workbench[0].get_inventory()['apps'][0]['visibility']=='public'
    else:
        assert r['verified'] is True
        assert r['question']['choices']==['keep_restricted','undo']
        r=answer(g,cfg,r,'keep_restricted')
        assert deps.store.get_case(cid)['status']=='resolved'
        assert workbench[0].get_inventory()['apps'][0]['visibility']=='company'


def test_public_views_contain_then_undo_escalates(tmp_path,workbench):
    deps,g,cid,cfg,r=setup(tmp_path,workbench,leaking=True)
    assert r['verified']
    r=answer(g,cfg,r,'undo')
    assert deps.store.get_case(cid)['status']=='needs_review'
    assert deps.store.has_human_override('helio',r['finding_id'])
    assert workbench[0].get_inventory()['apps'][0]['visibility']=='public'
    cid2=deps.store.open_case('helio',r['finding_id'],route='investigating',now=2000)
    r2=asyncio.run(g.ainvoke({'finding_id':r['finding_id'],'company_id':'helio','facts':r['facts']},{'configurable':{'thread_id':cid2}}))
    assert r2['question']['role']=='approver'
    assert deps.store.get_actions(cid2)==[]

@pytest.mark.parametrize('choice',['remove_connection','keep_paused','allow_7_days'])
def test_external_send_approver_paths(tmp_path,workbench,choice):
    deps,g,cid,cfg,r=setup(tmp_path,workbench,AGENT_RULE,leaking=True)
    assert r['verified']
    assert r['question']['choices']==['remove_connection','keep_paused','allow_7_days']
    assert any(m[1]=='workbench_owner_notice' for m in deps.notifier.messages)
    r=answer(g,cfg,r,choice)
    assert deps.store.get_case(cid)['status']=='resolved'
    agent=next(a for a in workbench[0].get_inventory()['agents'] if a['id']=='agent-lead-enrichment')
    assert agent['status']==('paused' if choice=='keep_paused' else 'active')
    assert agent['destinations'][0]['enabled']==(choice!='remove_connection')
    if choice=='allow_7_days':
        assert deps.platform_store.finding('helio',r['finding_id'],now=1000)['reason']=='exception_active'

@pytest.mark.parametrize('employee,choice', [('emp-zara-ali','allow_7_days'),('system','timeout')])
def test_no_unauthorized_or_early_timeout_mutation(tmp_path,workbench,employee,choice):
    deps,g,cid,cfg,r=setup(tmp_path,workbench)
    answer(g,cfg,r,choice,employee)
    assert deps.store.get_case(cid)['status']=='needs_review'
    assert deps.store.get_actions(cid)==[]


def test_finding_to_facts_uses_workbench_evidence():
    from helio_security.workflow import facts_from_finding
    f=facts_from_finding({'rule':APP_RULE,'evidence':{'employee_id':'luca','employee_status':'active','anonymous_views_1h':1,'app_id':'app'}})
    assert (f['target_kind'],f['exposure'],f['leaking_now'],f['addressed_to'])==('app','public',True,'luca')


def test_expired_approval_is_not_silently_renewed_during_recovery(tmp_path,workbench):
    deps,g,cid,cfg,r=setup(tmp_path,workbench)
    q=r['question']
    deps.store.record_decision(cid,key=q['key'],choice='intentional',employee_id=q['addressed_to'],source='test',now=1001)
    deps.clock=lambda:1001+7*86400+1
    answer(g,cfg,r,'intentional')
    assert deps.store.get_case(cid)['status']=='needs_review'
    assert deps.platform_store.finding('helio',r['finding_id'],now=deps.clock())['reason']!='exception_active'


def test_failed_destination_removal_never_resumes_agent(tmp_path,workbench,monkeypatch):
    from helio_security.playbooks import PlaybookError
    deps,g,cid,cfg,r=setup(tmp_path,workbench,AGENT_RULE,leaking=True)
    async def reject(*args,**kwargs): raise PlaybookError('stale version')
    monkeypatch.setattr(deps.playbooks['workbench.disable_destination'],'run',reject)
    answer(g,cfg,r,'remove_connection')
    assert deps.store.get_case(cid)['status']=='needs_review'
    assert workbench[0].get_agent('agent-lead-enrichment')['status']=='paused'
    assert not any(a['undo_of'] for a in deps.store.get_actions(cid))

@pytest.mark.parametrize('choice',['remove_connection','allow_7_days'])
def test_approval_for_one_destination_does_not_resume_other_unapproved_sends(tmp_path,workbench,choice):
    deps,g,cid,cfg,r=setup(tmp_path,workbench,AGENT_RULE,leaking=True)
    workbench[0].add_destination('agent-lead-enrichment','another-outside','http://another-outside/collect')
    answer(g,cfg,r,choice)
    assert workbench[0].get_agent('agent-lead-enrichment')['status']=='paused'
    assert deps.store.get_case(cid)['status']=='needs_review'
    assert not any(a['undo_of'] for a in deps.store.get_actions(cid))
