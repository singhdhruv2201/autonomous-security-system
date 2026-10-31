"""Directory -> authenticated publication -> CRM enforcement over real HTTP."""
import secrets
import urllib.error

import pytest

from helio_crm.api import create_app as crm_app
from helio_crm.policies import PolicyConfig
from helio_crm.store import CRMStore
from helio_simulator.api import create_app as directory_app
from test_graph import serve
from test_activity_end_to_end import get


def test_publisher_propagates_offboarding_and_outage_does_not_refresh_evidence(tmp_path,directory):
    from helio_security.policy_worker import IdentityPublisher
    tokens={k:secrets.token_urlsafe(32) for k in ('admin','maya','agent','publisher')}
    now=[1000.0]
    crm=CRMStore(tmp_path/'crm.db',clock=lambda:now[0])
    with serve(directory_app(directory.path)) as directory_url:
        with serve(crm_app(crm.path,tokens,directory.employees,policy_config=PolicyConfig(owner_mode='enforce'),clock=lambda:now[0])) as crm_url:
            publisher=IdentityPublisher(directory_url,crm_url,tokens['publisher'],clock=lambda:now[0])
            publisher.run_once()
            assert len(get(crm_url+'/customers',tokens['agent']))==6
            directory.set_employee_status('emp-maya-chen','inactive')
            now[0]+=1
            publisher.run_once()
            with pytest.raises(urllib.error.HTTPError) as caught: get(crm_url+'/customers',tokens['agent'])
            assert caught.value.code==403
            with pytest.raises(urllib.error.HTTPError): get(crm_url+'/customers',tokens['publisher'])
            now[0]+=121
            with pytest.raises(urllib.error.HTTPError) as caught: get(crm_url+'/customers',tokens['agent'])
            assert caught.value.code==503


def test_failed_publication_retries_soon_and_does_not_block_other_jobs(tmp_path):
    from unittest.mock import Mock
    from helio_security.policy_worker import PolicyWorker
    from helio_security.platform_store import PlatformStore
    store=PlatformStore(tmp_path/'platform.db');store.initialize()
    now=[1000.0]
    publisher=Mock();publisher.run_once.side_effect=[OSError(),1]
    worker=PolicyWorker(store,'helio',publisher=publisher,clock=lambda:now[0])
    worker.run_once()
    assert store.runtime('helio')['identity_publisher']['value']['status']=='unavailable'
    now[0]+=5;worker.run_once()
    assert publisher.run_once.call_count==2
    assert store.runtime('helio')['identity_publisher']['value']['status']=='ok'
