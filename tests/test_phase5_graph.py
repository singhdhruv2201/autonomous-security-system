import copy
import secrets
import time

import pytest

from helio_security.models import normalize
from helio_security.platform_store import PlatformStore
from helio_workbench.store import WorkbenchStore
from helio_crm.store import CRMStore
from test_graph import graph

APP_RULE = 'public_app_confidential_data'
AGENT_RULE = 'agent_unapproved_destination'

@pytest.fixture
def inventory(tmp_path):
    return WorkbenchStore(tmp_path/'wb.sqlite3').get_inventory()


def test_workbench_normalization_keeps_links_and_only_metadata(inventory):
    inventory['apps'][0]['notes'] = 'private customer value'
    snap = normalize('workbench', inventory)
    assert {n['kind'] for n in snap.nodes} >= {'App','Agent','Connection','Destination'}
    assert {e['kind'] for e in snap.edges} >= {'OWNS','ACTS_AS','HAS_CONNECTION','TO'}
    assert 'private customer value' not in str(snap)
    assert any(e['end']==['ServiceAccount','service:renewal-tracker'] for e in snap.edges)
    inventory['apps'][0]['visibility']='public'
    assert any(e['kind']=='EXPOSED_TO' for e in normalize('workbench',inventory).edges)


@pytest.mark.parametrize('change', ['duplicate','incomplete','bad_destination'])
def test_invalid_workbench_snapshot_rejected(inventory,change):
    if change=='duplicate': inventory['apps'].append(inventory['apps'][0])
    if change=='incomplete': inventory['complete']=False
    if change=='bad_destination': inventory['agents'][1]['destinations'][0]['enabled']='yes'
    with pytest.raises(ValueError): normalize('workbench',inventory)


def row():
    return dict(employee_id='owner',employee_status='active',app_id='app',agent_id='agent',
        grant_id='grant',resource_id='data',host='enrich-leads',present=True,
        grant_status='active',principal_status='active',classification='confidential',
        scopes=['customers:read'],visibility='public',agent_status='active',enabled=True)

@pytest.mark.parametrize('rule,changes,state,reason',[
    (APP_RULE,{},'open','public_confidential_access'),
    (APP_RULE,{'visibility':'company'},'resolved','app_restricted'),
    (APP_RULE,{'visibility':'private'},'resolved','app_restricted'),
    (APP_RULE,{'grant_status':'revoked'},'resolved','grant_revoked'),
    (APP_RULE,{'principal_status':'disabled'},'resolved','principal_disabled'),
    (APP_RULE,{'classification':'internal'},'resolved','resource_not_confidential'),
    (APP_RULE,{'scopes':[]},'resolved','no_customer_access_scope'),
    (APP_RULE,{'present':False},'unknown','missing_evidence'),
    (AGENT_RULE,{},'open','unapproved_destination'),
    (AGENT_RULE,{'agent_status':'paused'},'resolved','agent_paused'),
    (AGENT_RULE,{'enabled':False},'resolved','connection_disabled'),
    (AGENT_RULE,{'host':'partner-api'},'resolved','destination_approved'),
])
def test_rule_assessment(rule,changes,state,reason):
    from helio_security.rules import RULES
    value=RULES[rule].assess({**row(),**changes},True,{'partner-api'})
    assert (value['state'],value['reason'])==(state,reason)
    assert RULES[rule].assess(row(),False,{'partner-api'})['state']=='unknown'


def evaluation(rule,at=100):
    from helio_security.rules import RULES
    return dict(rule=rule,observed_at=at,coverage_complete=True,
                sources={s:{'last_success':at} for s in RULES[rule].sources},
                candidates=[RULES[rule].assess(row(),True,{'partner-api'})])

def finish(store,ev,at):
    run,_=store.enqueue('helio','test','manual',at)
    store.claim('helio',at)
    store.finish('helio',run['id'],{s:'ok' for s in ('directory','crm','workbench')},ev,at)


def test_exception_is_company_scoped_durable_expires_and_reopens(tmp_path):
    store=PlatformStore(tmp_path/'p.db');store.initialize()
    finish(store,evaluation(APP_RULE),100)
    finding=store.findings('helio',now=100)['items'][0]
    store.approve_exception('helio',finding['id'],'emp-casey-wong',expires_at=110,now=101)
    assert store.finding('helio',finding['id'],now=101)['reason']=='exception_active'
    store.initialize()
    finish(store,evaluation(APP_RULE,105),105)
    assert store.finding('helio',finding['id'],now=105)['status']=='resolved'
    finish(store,evaluation(APP_RULE,111),111)
    assert store.finding('helio',finding['id'],now=111)['status']=='open'
    assert store.finding('helio',finding['id'],now=111)['events'][-1]['type']=='reopened'
    with pytest.raises(ValueError):
        store.approve_exception('other',finding['id'],'emp-casey-wong',expires_at=120,now=112)


@pytest.mark.neo4j
def test_real_graph_rules_controls_freshness_and_resolution(graph,directory,tmp_path):
    from helio_security.reader import GraphReader
    tokens={k:secrets.token_urlsafe(32) for k in ('admin','maya','agent','observer','renewal_tracker','support_lookup','lead_enrichment','renewal_digest')}
    crm=CRMStore(tmp_path/'crm.db');crm.initialize(directory.employees(),tokens)
    wb=WorkbenchStore(tmp_path/'wb.db')
    at=time.time()
    def sync(at):
        for source,payload in [('directory',directory.snapshot()),('crm',crm.inventory(tokens['observer'])),('workbench',wb.get_inventory())]:
            graph.apply(normalize(source,payload),at)
    sync(at)
    reader=GraphReader(graph)
    def ev(at): return {r['rule']:r for r in reader.evaluate(at)['evaluations']}
    assert not any(c['state']=='open' for r in ev(at).values() for c in r['candidates'])
    wb.update_app('app-renewal-tracker',1,visibility='public')
    wb.add_destination('agent-lead-enrichment','enrich-leads','http://enrich-leads:8005/collect')
    sync(at+1)
    result=ev(at+1)
    for rule in (APP_RULE,AGENT_RULE):
        assert len([c for c in result[rule]['candidates'] if c['state']=='open'])==1
    graph.fail('workbench','unavailable',at+2)
    result=ev(at+2)
    assert result['inactive_owner_agent_access']['coverage_complete']
    assert not result[APP_RULE]['coverage_complete']
    wb.update_app('app-renewal-tracker',2,visibility='company')
    wb.update_destination('agent-lead-enrichment','enrich-leads',1,enabled=False)
    sync(at+3)
    result=ev(at+3)
    assert any(c['reason']=='app_restricted' for c in result[APP_RULE]['candidates'])
    assert any(c['reason']=='connection_disabled' for c in result[AGENT_RULE]['candidates'])


def test_workbench_connector_reads_real_authenticated_inventory(tmp_path):
    from helio_workbench.api import create_app
    from helio_security.connectors import fetch_snapshot, ConnectorError
    from test_graph import serve
    app=create_app(store=WorkbenchStore(tmp_path/'api.db'),observer_token='o'*32,action_token='a'*32,user_token='u'*32)
    with serve(app) as url:
        snapshot=fetch_snapshot('workbench',url,'o'*32)
        assert snapshot.source=='workbench'
        with pytest.raises(ConnectorError): fetch_snapshot('workbench',url,'wrong')


def test_source_list_worker_keeps_unrelated_rule_fresh(tmp_path):
    from unittest.mock import Mock
    from helio_security.worker import ObservationWorker
    from test_platform_store import evaluation as inactive
    store=PlatformStore(tmp_path/'worker.db');store.initialize()
    reader=Mock()
    reader.evaluate.return_value={'evaluations':[inactive(100),evaluation(APP_RULE)],'coverage_complete':True}
    sync=Mock(return_value={'directory':'ok','crm':'ok','workbench':'unavailable'})
    worker=ObservationWorker(store,Mock(company_id='helio'),reader,'directory','crm','obs',
                             workbench_url='workbench',workbench_token='wb-obs',sync=sync,clock=lambda:100)
    run,_=store.enqueue('helio','test','manual',99)
    worker.run_once()
    assert store.findings('helio',now=100)['total']==1
    assert store.findings('helio',now=100)['items'][0]['rule']=='inactive_owner_agent_access'
    sync.assert_called_once_with(worker.graph,[('directory','directory',None),('crm','crm','obs'),('workbench','workbench','wb-obs')])
