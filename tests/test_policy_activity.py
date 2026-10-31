import json
import secrets
import time

import pytest

from helio_crm.store import CRMStore, CRMError
from helio_crm.policies import PolicyConfig
from helio_security.activity_store import ActivityStore
from helio_security.platform_store import PlatformStore
from test_crm_activity import events


def test_decisions_become_durable_incidents_exactly_once_despite_replay_and_old_evidence(tmp_path,directory):
    tokens={k:secrets.token_urlsafe(32) for k in ('admin','maya','agent')}
    crm=CRMStore(tmp_path/'crm.db',activity_enabled=True,policy_config=PolicyConfig(content_mode='enforce'))
    crm.initialize(directory.employees(),tokens)
    with pytest.raises(CRMError): crm.update_customer(tokens['maya'],'cust-northstar',1,{'notes':'sk_live_'+'x'*24})
    inbox=ActivityStore(tmp_path/'inbox.db'); inbox.initialize()
    for event in events(crm): inbox.accept(event,time.time())
    while inbox.process_one(): pass
    page=inbox.policy_events('helio',after=0)
    platform=PlatformStore(tmp_path/'platform.db'); platform.initialize()
    platform.import_activity('helio',page,time.time())
    platform.import_activity('helio',page,time.time())
    items=platform.findings('helio',now=time.time()+10000)['items']
    assert len(items)==1 and items[0]['rule']=='sensitive_content'
    assert items[0]['assessment']=='current'
    assert platform.finding('helio',items[0]['id'])['event_count']==1
    assert 'sk_live_' not in json.dumps(items)
    platform.initialize()
    assert platform.activity_cursor('helio')==page['next_after']
    bad={**page,'instance_id':'00000000-0000-0000-0000-000000000000'}
    with pytest.raises(ValueError): platform.import_activity('helio',bad,time.time())


def test_invalid_cross_company_policy_page_does_not_advance_cursor(tmp_path):
    platform=PlatformStore(tmp_path/'platform.db');platform.initialize()
    with pytest.raises(ValueError):
        platform.import_activity('helio',{'company_id':'other'},time.time())
    assert platform.activity_cursor('helio')==0


def test_policy_worker_imports_over_authorized_http_without_graph(tmp_path,directory):
    from helio_security.policy_worker import PolicyWorker
    from helio_security.webhooks import create_app
    from helio_security.activity_client import ActivityClient
    from test_graph import serve
    from unittest.mock import Mock
    tokens={k:secrets.token_urlsafe(32) for k in ('admin','maya','agent')}
    crm=CRMStore(tmp_path/'crm.db',activity_enabled=True);crm.initialize(directory.employees(),tokens)
    with pytest.raises(CRMError): crm.list_grants(tokens['agent'])
    viewer,operator,secret=[secrets.token_urlsafe(32) for _ in range(3)]
    app=create_app(tmp_path/'inbox.db',{'key':dict(secret=secret,company_id='helio',source='helio-crm')},read_tokens=dict(viewer=viewer,operator=operator))
    platform=PlatformStore(tmp_path/'platform.db');platform.initialize()
    with serve(app) as url:
        for event in events(crm): app.state.inbox.accept(event,time.time())
        while app.state.inbox.process_one(): pass
        worker=PolicyWorker(platform,'helio',activity_client=ActivityClient(url,viewer))
        worker.run_once()
        worker.run_once()
        assert platform.findings('helio')['total']==1
        assert platform.runtime('helio')['activity_policy_worker']['value']['status']=='ok'


def test_ingestion_health_defers_policy_enablement_to_crm_configuration(tmp_path):
    inbox=ActivityStore(tmp_path/'inbox.db');inbox.initialize()
    health=inbox.health('helio')
    assert health['scanners']=='per_request'
    assert health['policies_enabled'] is None


def test_cooldown_attempts_share_one_bounded_incident(tmp_path,directory):
    tokens={k:secrets.token_urlsafe(32) for k in ('admin','maya','agent')}
    crm=CRMStore(tmp_path/'crm.db',activity_enabled=True,policy_config=PolicyConfig(failure_mode='enforce',failure_limit=2),clock=lambda:1000.0)
    crm.initialize(directory.employees(),tokens)
    for _ in range(6):
        with pytest.raises(CRMError):crm.list_customers('same-invalid-credential')
    inbox=ActivityStore(tmp_path/'inbox.db');inbox.initialize()
    for event in events(crm):inbox.accept(event,time.time())
    while inbox.process_one():pass
    platform=PlatformStore(tmp_path/'platform.db');platform.initialize()
    platform.import_activity('helio',inbox.policy_events('helio'),time.time())
    assert len([f for f in platform.findings('helio')['items'] if f['rule']=='authentication_failures'])==1
