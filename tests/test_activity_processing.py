import json
import sqlite3
import time
import uuid

import pytest

from test_activity_recovery import recovery
from test_crm_activity import events
from test_graph import serve


def make_inbox(tmp_path):
    from helio_security.activity_store import ActivityStore
    inbox = ActivityStore(tmp_path/'inbox.sqlite3')
    inbox.initialize()
    return inbox


def test_activity_store_import_does_not_require_web_app_startup():
    import os
    import subprocess
    result = subprocess.run(['.venv/bin/python','-c','from helio_security.activity_store import ActivityStore'],
                            env={**os.environ,'PYTHONPATH':'src'},capture_output=True,text=True)
    assert result.returncode == 0, result.stderr


def test_out_of_order_checkpoints_and_atomic_processing_restart(tmp_path, recovery):
    from helio_security.activity_store import ActivityStore
    _, crm, tokens = recovery
    for _ in range(3):
        crm.list_customers(tokens['agent'])
    rows = events(crm)
    inbox = make_inbox(tmp_path)
    inbox.accept(rows[2], time.time())
    assert inbox.health('helio')['sources'][0]['received_cursor'] == 0
    assert inbox.process_one()
    assert inbox.health('helio')['sources'][0]['processed_cursor'] == 0
    for row in rows[:2]:
        inbox.accept(row, time.time())
    assert inbox.health('helio')['sources'][0]['received_cursor'] == 3
    with inbox.connect() as db:
        db.execute("CREATE TRIGGER fail_normalize BEFORE INSERT ON normalized_events BEGIN SELECT RAISE(ABORT,'private'); END")
    with pytest.raises(sqlite3.Error):
        inbox.process_one()
    assert inbox.health('helio')['sources'][0]['processed_cursor'] == 0
    with inbox.connect() as db:
        db.execute('DROP TRIGGER fail_normalize')
    restarted = ActivityStore(inbox.path)
    restarted.initialize()
    assert restarted.process_one() and restarted.process_one() and not restarted.process_one()
    assert restarted.health('helio')['sources'][0]['processed_cursor'] == 3
    assert restarted.activity('helio')['total'] == 3
    assert restarted.accept(rows[0], time.time()) == 200
    assert not restarted.process_one()


def test_existing_phase3_inbox_migrates_without_losing_pending_receipts(tmp_path, recovery):
    from helio_security.webhooks import Inbox
    _, crm, tokens = recovery
    crm.list_customers(tokens['agent'])
    old = Inbox(tmp_path/'inbox.sqlite3')
    old.initialize()
    old.accept(events(crm)[0], time.time())
    inbox = make_inbox(tmp_path)
    assert inbox.process_one()
    assert inbox.activity('helio')['total'] == 1
    assert inbox.health('helio')['sources'][0]['received_cursor'] == 1


def test_missing_middle_and_final_events_recovered_via_real_http(tmp_path, directory):
    import secrets
    from helio_crm.api import create_app
    from helio_crm.store import CRMStore
    from helio_security.activity_worker import ActivityWorker, RecoveryClient
    tokens = {name:secrets.token_urlsafe(32) for name in ('admin','maya','agent','recovery')}
    path = tmp_path/'crm.sqlite3'
    inbox = make_inbox(tmp_path)
    with serve(create_app(path,tokens,directory.employees,activity_enabled=True)) as url:
        crm = CRMStore(path,activity_enabled=True)
        for _ in range(4):
            crm.list_customers(tokens['agent'])
        rows = events(crm)
        inbox.accept(rows[0],time.time())
        inbox.accept(rows[2],time.time())
        worker = ActivityWorker(inbox,RecoveryClient(url,tokens['recovery']))
        worker.run_once()
        health = inbox.health('helio')
        assert health['coverage_complete'] is True
        assert health['sources'][0]['received_cursor'] == health['sources'][0]['processed_cursor'] == 4
        assert inbox.activity('helio')['total'] == 4
        # No later webhook signals this missing final event: polling must find it.
        crm.list_customers(tokens['agent'])
        ActivityWorker(inbox,RecoveryClient(url,tokens['recovery'])).run_once()
        assert inbox.activity('helio')['total'] == 5


def test_reset_stale_poll_and_expired_history_stay_visible(tmp_path, recovery):
    client, crm, tokens = recovery
    from test_activity_recovery import auth
    inbox = make_inbox(tmp_path)
    crm.list_customers(tokens['agent'])
    head = client.get('/activity/status',headers=auth(tokens['recovery'])).json()
    inbox.observe(head, 1000)
    assert inbox.health('helio',now=1000)['sources'][0]['missing_ranges'] == [[1,1]]
    inbox.accept(events(crm)[0],1000)
    inbox.process_one()
    assert inbox.health('helio',now=1000)['coverage_complete'] is True
    assert inbox.health('helio',now=1100)['coverage_complete'] is False
    head.update(source_instance_id=str(uuid.uuid4()),high_watermark=3,earliest_available=3)
    inbox.observe(head,1100)
    health = inbox.health('helio',now=1100)
    assert health['source_resets'] == 1 and not health['coverage_complete']
    active = next(s for s in health['sources'] if s['active'])
    assert active['history_expired'] and active['received_cursor'] == 0


def test_activity_groups_stages_and_scopes_pagination_and_filters(tmp_path, recovery):
    client, crm, tokens = recovery
    from test_activity_recovery import auth
    client.get('/customers',headers=auth(tokens['agent']))
    client.get('/customers')
    inbox = make_inbox(tmp_path)
    for row in events(crm):
        inbox.accept(row,time.time())
    while inbox.process_one():
        pass
    page = inbox.activity('helio',limit=1)
    assert page['total'] == 2 and len(page['items']) == 1
    assert len(page['items'][0]['stages']) == 4
    assert page['items'][0]['scan_status'] == 'not_run'
    next_page = inbox.activity('helio',limit=1,before=page['next_before'],snapshot=page['snapshot'])
    assert next_page['items'][0]['request_id'] != page['items'][0]['request_id']
    assert inbox.activity('other')['total'] == 0
    assert inbox.activity('helio',outcome='denied')['total'] == 1
    assert inbox.activity('helio',actor_id='agent:sales-research')['total'] == 1
    assert tokens['agent'] not in json.dumps(page)


def test_initial_connector_failure_has_a_visible_safe_reason(tmp_path):
    from helio_security.activity_worker import ActivityWorker, RecoveryError
    class Offline:
        def get(self, *args, **kwargs):
            raise RecoveryError('unavailable')
    inbox = make_inbox(tmp_path)
    ActivityWorker(inbox,Offline()).run_once()
    health = inbox.health('helio')
    assert not health['coverage_complete'] and health['recovery_error'] == 'unavailable'


@pytest.mark.parametrize('fault', ['gap','scope','cursor','conflict'])
def test_recovery_refuses_invalid_pages_without_advancing_checkpoint(tmp_path,recovery,fault):
    from helio_security.activity_worker import ActivityWorker
    from test_activity_recovery import auth
    client, crm, tokens = recovery
    for _ in range(2):
        crm.list_customers(tokens['agent'])
    head = client.get('/activity/status',headers=auth(tokens['recovery'])).json()
    page = client.get('/activity/events',headers=auth(tokens['recovery'])).json()
    inbox = make_inbox(tmp_path)
    if fault=='gap':
        page['items']=page['items'][1:]
    elif fault=='scope':
        page['items'][0]['company_id']='other'
    elif fault=='cursor':
        page['next_after']=99
    elif fault=='conflict':
        conflict={**page['items'][1],'result_record_count':99}
        inbox.accept(conflict,time.time())
    class Source:
        def get(self,path,**params):
            return head if path.endswith('status') else page
    ActivityWorker(inbox,Source()).run_once()
    health=inbox.health('helio')
    assert not health['coverage_complete']
    assert health['recovery_error']
    if fault == 'conflict':
        ActivityWorker(inbox,Source()).run_once()
        assert not inbox.health('helio')['coverage_complete']
        assert inbox.health('helio')['recovery_error'] == 'identity_conflict'
