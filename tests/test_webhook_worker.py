import secrets
import pytest
from test_crm_activity import activity_crm, events
from test_graph import serve
from test_webhooks import receiver


def test_real_http_delivery_and_restart(activity_crm,tmp_path):
    from helio_crm.webhook_worker import DeliveryWorker
    store,tokens = activity_crm
    store.list_customers(tokens['agent'])
    key = secrets.token_urlsafe(32)
    with serve(receiver(tmp_path,key)) as url:
        worker = DeliveryWorker(store,url+'/webhooks/crm/activity','crm-1',key,allow_local=True)
        assert worker.run_once()
        assert not worker.run_once()
        again = DeliveryWorker(store,url+'/webhooks/crm/activity','crm-1',key,allow_local=True)
        assert not again.run_once()
    with store.connection() as db:
        assert db.execute('SELECT status FROM activity_outbox').fetchone()[0] == 'delivered'
    assert len(events(store)) == 1


def test_retry_dead_letter_and_replay(activity_crm):
    from helio_crm.webhook_worker import DeliveryWorker
    store,tokens = activity_crm
    store.list_customers(tokens['agent'])
    now = [1000.]
    worker = DeliveryWorker(store,'https://example.test/webhooks/crm/activity','crm-1',secrets.token_urlsafe(32),
                            clock=lambda:now[0],transport=lambda *a:(503,None),max_attempts=2,jitter=lambda:0)
    assert worker.run_once()
    assert not worker.run_once()
    now[0] += 100
    assert worker.run_once()
    with store.connection() as db:
        assert db.execute('SELECT status,attempts FROM activity_outbox').fetchone()[:] == ('dead_letter',2)
    assert not worker.run_once()
    with pytest.raises(Exception): store.replay_delivery(tokens['agent'],1)
    store.replay_delivery(tokens['admin'],1)
    assert worker.run_once()


def test_expired_claim_is_recovered_after_sender_crash(activity_crm):
    from helio_crm.webhook_worker import DeliveryWorker
    store,tokens = activity_crm
    store.list_customers(tokens['agent'])
    now=[1000.]
    worker=DeliveryWorker(store,'https://example.test/webhooks/crm/activity','crm-1',secrets.token_urlsafe(32),clock=lambda:now[0],transport=lambda *a:(202,None))
    claimed=worker.claim()
    assert claimed and not worker.run_once()
    now[0]+=61
    assert worker.run_once()
    with store.connection() as db: assert db.execute('SELECT status FROM activity_outbox').fetchone()[0]=='delivered'


@pytest.mark.parametrize('status,expected',[(200,'delivered'),(202,'delivered'),(204,'dead_letter'),(301,'dead_letter'),(401,'dead_letter'),(403,'dead_letter'),(409,'dead_letter'),(413,'dead_letter'),(422,'dead_letter'),(429,'pending'),(500,'pending'),(0,'pending')])
def test_status_classification_and_retry_after(activity_crm,status,expected):
    from helio_crm.webhook_worker import DeliveryWorker
    store,tokens=activity_crm
    store.list_customers(tokens['agent'])
    w=DeliveryWorker(store,'https://example.test/webhooks/crm/activity','crm-1','x'*32,clock=lambda:1000,transport=lambda *a:(status,'999999999'),jitter=lambda:0)
    assert w.run_once()
    with store.connection() as db:
        row=db.execute('SELECT * FROM activity_outbox').fetchone()
        assert row['status']==expected
        assert row['next_attempt_at']<=1300
        assert db.execute('SELECT count(*) FROM delivery_audit').fetchone()[0]==1
    assert len(events(store))==1


def test_acknowledgement_loss_retries_same_event_without_duplicate(tmp_path,activity_crm):
    from helio_crm.webhook_worker import DeliveryWorker,deliver
    store,tokens=activity_crm
    store.list_customers(tokens['agent'])
    key=secrets.token_urlsafe(32)
    import time,sqlite3
    now=[time.time()]
    calls=[]
    def lose_ack(url,body,headers):
        response=deliver(url,body,headers)
        calls.append(response[0])
        if len(calls)==1: raise OSError('lost response')
        return response
    with serve(receiver(tmp_path,key)) as url:
        w=DeliveryWorker(store,url+'/webhooks/crm/activity','crm-1',key,allow_local=True,clock=lambda:now[0],transport=lose_ack)
        assert w.run_once()
        now[0]+=10
        assert w.run_once()
    assert calls==[202,200]
    with sqlite3.connect(tmp_path/'inbox.sqlite3') as db: assert db.execute('SELECT count(*) FROM inbox').fetchone()[0]==1


def test_concurrent_claims_and_stale_sender_cannot_overwrite_delivery(activity_crm):
    from concurrent.futures import ThreadPoolExecutor
    from helio_crm.webhook_worker import DeliveryWorker
    store,tokens=activity_crm
    store.list_customers(tokens['agent'])
    now=[1000.]
    w=DeliveryWorker(store,'https://example.test/webhooks/crm/activity','crm-1','x'*32,clock=lambda:now[0],transport=lambda *a:(202,None))
    with ThreadPoolExecutor(max_workers=4) as pool: claims=list(pool.map(lambda _:w.claim(),range(4)))
    [old]=[c for c in claims if c]
    now[0]+=61
    assert w.run_once()
    w.finish(old,500,None)
    with store.connection() as db: assert db.execute('SELECT status FROM activity_outbox').fetchone()[0]=='delivered'


@pytest.mark.parametrize('url',['http://example.test/webhooks/crm/activity','https://user:secret@example.test/webhooks/crm/activity','https://example.test/webhooks/crm/activity?token=secret','https://example.test/other','file:///tmp/events'])
def test_bad_destinations_are_rejected(activity_crm,url):
    from helio_crm.webhook_worker import DeliveryWorker
    with pytest.raises(ValueError): DeliveryWorker(activity_crm[0],url,'crm-1','x'*32,allow_local=True)


def test_new_worker_recovers_but_does_not_reset_exhausted_attempts(activity_crm):
    from helio_crm.webhook_worker import DeliveryWorker
    store,tokens=activity_crm
    store.list_customers(tokens['agent'])
    now=[1000.]
    w=DeliveryWorker(store,'https://example.test/webhooks/crm/activity','crm-1','x'*32,clock=lambda:now[0],max_attempts=1)
    assert w.claim()
    now[0]+=61
    assert not w.run_once()
    with store.connection() as db: assert db.execute('SELECT status FROM activity_outbox').fetchone()[0]=='dead_letter'


def test_setup_adds_distinct_webhook_key_without_rotating_existing_credentials(tmp_path):
    from helio_security.setup import setup
    path=tmp_path/'.env'
    setup(path)
    before=path.read_text()
    settings=dict(line.split('=',1) for line in before.splitlines())
    assert len(settings['CRM_WEBHOOK_SECRET'])>=32
    assert len(set(settings.values()))==len(settings)
    setup(path)
    assert path.read_text()==before and path.stat().st_mode & 0o777==0o600


def test_replay_audits_authorized_actor_and_exact_target(activity_crm):
    from helio_crm.store import CRMError
    store,tokens=activity_crm
    store.list_customers(tokens['agent'])
    with pytest.raises(CRMError) as error: store.replay_delivery(tokens['admin'],1)
    assert error.value.status_code==409
    with store.connection() as db: db.execute("UPDATE activity_outbox SET status='dead_letter' WHERE event_sequence=1")
    store.replay_delivery(tokens['admin'],1)
    with store.connection() as db:
        row=db.execute("SELECT actor_id,event_sequence,outcome FROM delivery_audit WHERE outcome='replayed'").fetchone()
        assert tuple(row)==('service:crm-admin',1,'replayed')


def test_background_delivery_of_complete_http_lifecycle(tmp_path,directory,monkeypatch):
    import sqlite3,time,urllib.request
    from helio_crm.api import create_app
    from helio_crm.store import CRMStore
    tokens={key:secrets.token_urlsafe(32) for key in ('admin','maya','agent')}
    key=secrets.token_urlsafe(32)
    with serve(receiver(tmp_path,key)) as receiver_url:
        monkeypatch.setenv('CRM_WEBHOOK_ENABLED','1')
        monkeypatch.setenv('CRM_WEBHOOK_ALLOW_LOCAL','1')
        monkeypatch.setenv('CRM_WEBHOOK_URL',receiver_url+'/webhooks/crm/activity')
        monkeypatch.setenv('CRM_WEBHOOK_SECRET',key)
        with serve(create_app(tmp_path/'crm.sqlite3',tokens,directory.employees,activity_enabled=True)) as crm_url:
            req=urllib.request.Request(crm_url+'/customers',headers={'Authorization':'Bearer '+tokens['agent']})
            with urllib.request.urlopen(req,timeout=5) as response: assert response.status==200
            deadline=time.monotonic()+5
            while time.monotonic()<deadline:
                with sqlite3.connect(tmp_path/'inbox.sqlite3') as db: count=db.execute('SELECT count(*) FROM inbox').fetchone()[0]
                if count==4: break
                time.sleep(0.01)
            assert count==4
    with sqlite3.connect(tmp_path/'inbox.sqlite3') as db:
        payloads=[row[0] for row in db.execute('SELECT payload FROM inbox')]
        assert all(tokens['agent'] not in p for p in payloads)
    assert len(events(CRMStore(tmp_path/'crm.sqlite3')))==4


def test_http_replay_rejects_unbounded_sequence(tmp_path,directory):
    from fastapi.testclient import TestClient
    from helio_crm.api import create_app
    tokens={key:secrets.token_urlsafe(32) for key in ('admin','maya','agent')}
    with TestClient(create_app(tmp_path/'crm.sqlite3',tokens,directory.employees)) as client:
        response=client.post('/activity-deliveries/'+str(2**100)+'/replay',json={},headers={'Authorization':'Bearer '+tokens['admin']})
        assert response.status_code==422


def test_v3_pending_queue_migrates_without_changing_evidence(activity_crm):
    from helio_crm.store import CRMStore
    store,tokens=activity_crm
    store.list_customers(tokens['agent'])
    original=events(store)
    with store.connection() as db:
        db.execute('DROP TABLE activity_outbox')
        db.execute('DROP TABLE delivery_audit')
        db.execute("CREATE TABLE activity_outbox(event_sequence INTEGER PRIMARY KEY REFERENCES activity_events(sequence),status TEXT NOT NULL DEFAULT 'pending' CHECK(status='pending'))")
        db.execute('INSERT INTO activity_outbox(event_sequence) VALUES (1)')
        db.execute("UPDATE metadata SET value='3' WHERE key='schema_version'")
    upgraded=CRMStore(store.path,activity_enabled=True)
    upgraded.initialize([],tokens)
    upgraded.initialize([],tokens)
    assert events(upgraded)==original
    with upgraded.connection() as db:
        assert tuple(db.execute('SELECT status,attempts,lease_id FROM activity_outbox').fetchone())==('pending',0,None)


@pytest.mark.parametrize('damage', ['DROP TABLE delivery_audit', 'ALTER TABLE activity_outbox DROP COLUMN attempts'])
def test_invalid_v4_schema_fails_startup_without_repairing_evidence(activity_crm, damage):
    store, tokens = activity_crm
    store.list_customers(tokens['agent'])
    original = events(store)
    with store.connection() as db:
        db.execute(damage)
    with pytest.raises(ValueError, match='activity schema'):
        store.initialize([], tokens)
    assert events(store) == original


def test_real_http_redirect_does_not_forward_signing_headers(activity_crm):
    from fastapi import FastAPI
    from fastapi.responses import RedirectResponse
    from helio_crm.webhook_worker import DeliveryWorker
    app, forwarded = FastAPI(), []
    @app.post('/webhooks/crm/activity')
    def redirect():
        return RedirectResponse('/tripwire', status_code=307)
    @app.post('/tripwire')
    def tripwire():
        forwarded.append(True)
        return {}
    store, tokens = activity_crm
    store.list_customers(tokens['agent'])
    with serve(app) as url:
        worker = DeliveryWorker(store, url+'/webhooks/crm/activity','crm-1','x'*32,allow_local=True)
        assert worker.run_once()
    assert forwarded == []
    with store.connection() as db:
        assert tuple(db.execute('SELECT status,last_status FROM activity_outbox').fetchone()) == ('dead_letter',307)


def test_receiver_outage_then_sender_restart_delivers_retained_event(tmp_path, activity_crm):
    import time
    from helio_crm.store import CRMStore
    from helio_crm.webhook_worker import DeliveryWorker
    store, tokens = activity_crm
    store.list_customers(tokens['agent'])
    key, now = secrets.token_urlsafe(32), [time.time()]
    with serve(receiver(tmp_path,key)) as url:
        pass  # Bound port now closed: a real connection failure, not a fake status.
    worker = DeliveryWorker(store,url+'/webhooks/crm/activity','crm-1',key,allow_local=True,clock=lambda:now[0])
    assert worker.run_once()
    with store.connection() as db:
        assert tuple(db.execute('SELECT status,last_status FROM activity_outbox').fetchone()) == ('pending',0)
    restarted = CRMStore(store.path, activity_enabled=True)
    restarted.initialize([], tokens)
    now[0] += 10
    with serve(receiver(tmp_path,key)) as new_url:
        again = DeliveryWorker(restarted,new_url+'/webhooks/crm/activity','crm-1',key,allow_local=True,clock=lambda:now[0])
        assert again.run_once()
    with restarted.connection() as db:
        assert tuple(db.execute('SELECT status,attempts FROM activity_outbox').fetchone()) == ('delivered',2)


def test_blocked_http_delivery_does_not_hold_crm_request_or_transaction(tmp_path, directory, monkeypatch):
    import threading
    from concurrent.futures import ThreadPoolExecutor
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from helio_crm.api import create_app
    entered, release = threading.Event(), threading.Event()
    app = FastAPI()
    @app.post('/webhooks/crm/activity', status_code=202)
    def wait_for_release():
        entered.set()
        release.wait(5)
        return {}
    tokens = {key:secrets.token_urlsafe(32) for key in ('admin','maya','agent')}
    with serve(app) as url:
        monkeypatch.setenv('CRM_WEBHOOK_ENABLED','1')
        monkeypatch.setenv('CRM_WEBHOOK_ALLOW_LOCAL','1')
        monkeypatch.setenv('CRM_WEBHOOK_URL',url+'/webhooks/crm/activity')
        monkeypatch.setenv('CRM_WEBHOOK_SECRET','x'*32)
        with TestClient(create_app(tmp_path/'crm.sqlite3',tokens,directory.employees,activity_enabled=True)) as client:
            with ThreadPoolExecutor(max_workers=1) as pool:
                try:
                    assert client.get('/customers',headers={'Authorization':'Bearer '+tokens['agent']}).status_code == 200
                    assert entered.wait(2)
                    future = pool.submit(client.patch,'/customers/cust-northstar',json={'expected_version':1,'notes':'saved'},headers={'Authorization':'Bearer '+tokens['maya']})
                    assert future.result(timeout=2).status_code == 200
                    assert not release.is_set()
                finally:
                    release.set()


def test_real_http_timeout_retains_event_for_retry(activity_crm):
    import threading
    from fastapi import FastAPI
    from helio_crm.webhook_worker import DeliveryWorker
    app, release = FastAPI(), threading.Event()
    @app.post('/webhooks/crm/activity', status_code=202)
    def stalled_receiver():
        release.wait(8)
        return {}
    store, tokens = activity_crm
    store.list_customers(tokens['agent'])
    with serve(app) as url:
        worker = DeliveryWorker(store,url+'/webhooks/crm/activity','crm-1','x'*32,allow_local=True)
        try:
            assert worker.run_once()
        finally:
            release.set()
    with store.connection() as db:
        assert tuple(db.execute('SELECT status,last_status,attempts FROM activity_outbox').fetchone()) == ('pending',0,1)
    assert len(events(store)) == 1
