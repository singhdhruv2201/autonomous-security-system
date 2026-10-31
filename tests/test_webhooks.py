import json
import secrets
import time
import uuid
import pytest
from fastapi.testclient import TestClient
from helio_crm.store import CRMStore
from test_crm_activity import activity_crm, events


def receiver(tmp_path, key):
    from helio_security.webhooks import create_app
    return create_app(tmp_path/'inbox.sqlite3', {'crm-1': {'secret':key, 'company_id':'helio', 'source':'helio-crm'}})


def post(client, event, key, **changes):
    from helio_security.webhooks import sign
    body = json.dumps(event).encode()
    headers = sign(body, 'crm-1', key, int(time.time()), str(uuid.uuid4()))
    headers.update(changes)
    return client.post('/webhooks/crm/activity', content=body, headers=headers)


def test_authenticated_receipt_is_durable_duplicate_safe_and_conflict_checked(tmp_path, activity_crm):
    store,tokens = activity_crm
    store.list_customers(tokens['agent'])
    event = events(store)[0]
    key = secrets.token_urlsafe(32)
    with TestClient(receiver(tmp_path,key)) as client:
        assert post(client,event,key).status_code == 202
        assert post(client,event,key).status_code == 200
        assert post(client,{**event,'result_record_count':99},key).status_code == 409
        assert post(client,{**event,'company_id':'another'},key).status_code == 403
    with TestClient(receiver(tmp_path,key)) as client:
        assert post(client,event,key).status_code == 200
    import sqlite3
    with sqlite3.connect(tmp_path/'inbox.sqlite3') as db:
        assert db.execute('SELECT count(*) FROM inbox').fetchone()[0] == 1
        assert db.execute('SELECT status FROM inbox').fetchone()[0] == 'pending'


@pytest.mark.parametrize('fault', ['signature','timestamp','schema','oversize'])
def test_receiver_rejects_invalid_inputs(tmp_path, activity_crm, fault):
    from helio_security.webhooks import sign
    store,tokens = activity_crm
    store.list_customers(tokens['agent'])
    event = events(store)[0]
    key = secrets.token_urlsafe(32)
    if fault == 'schema': event['unexpected'] = 'secret'
    body = json.dumps(event).encode() if fault != 'oversize' else b'x'*65537
    headers = sign(body,'crm-1',key,int(time.time())-(600 if fault=='timestamp' else 0),str(uuid.uuid4()))
    if fault == 'signature': body += b' '
    with TestClient(receiver(tmp_path,key)) as client:
        response = client.post('/webhooks/crm/activity',content=body,headers=headers)
    assert response.status_code == {'signature':401,'timestamp':401,'schema':422,'oversize':413}[fault]
    assert 'secret' not in response.text


def test_all_security_event_stages_and_replay_denials_are_accepted(tmp_path,activity_crm):
    from test_crm_lifecycle import run_middleware
    store,tokens=activity_crm
    async def app(scope,receive,send):
        store.list_customers(tokens['agent'])
        await send({'type':'http.response.start','status':200,'headers':[]})
        await send({'type':'http.response.body','body':b'[]','more_body':False})
    run_middleware(store,app)
    from helio_crm.store import CRMError
    with pytest.raises(CRMError): store.replay_delivery(tokens['agent'],1)
    key=secrets.token_urlsafe(32)
    with TestClient(receiver(tmp_path,key)) as client:
        for event in events(store): assert post(client,event,key).status_code==202


def test_rotation_wrong_key_replay_and_duplicate_json_fields(tmp_path,activity_crm):
    from helio_security.webhooks import create_app, sign
    store,tokens=activity_crm
    store.list_customers(tokens['agent'])
    event=events(store)[0]
    keys={k:{'secret':secrets.token_urlsafe(32),'company_id':'helio','source':'helio-crm'} for k in ('old','new')}
    body=json.dumps(event).encode()
    with TestClient(create_app(tmp_path/'inbox.sqlite3',keys)) as client:
        for key in keys:
            h=sign(body,key,keys[key]['secret'],int(time.time()),str(uuid.uuid4()))
            assert client.post('/webhooks/crm/activity',content=body,headers=h).status_code in (200,202)
        h=sign(body,'unknown',keys['old']['secret'],int(time.time()),str(uuid.uuid4()))
        assert client.post('/webhooks/crm/activity',content=body,headers=h).status_code==401
        duplicate=b'{"schema_version":1,'+body[1:]
        h=sign(duplicate,'new',keys['new']['secret'],int(time.time()),str(uuid.uuid4()))
        assert client.post('/webhooks/crm/activity',content=duplicate,headers=h).status_code==422
    with TestClient(create_app(tmp_path/'inbox.sqlite3',{'new':keys['new']})) as client:
        h=sign(body,'old',keys['old']['secret'],int(time.time()),str(uuid.uuid4()))
        assert client.post('/webhooks/crm/activity',content=body,headers=h).status_code==401


def test_inbox_storage_failure_does_not_acknowledge(tmp_path,activity_crm):
    import sqlite3
    store,tokens=activity_crm
    store.list_customers(tokens['agent'])
    key=secrets.token_urlsafe(32)
    with TestClient(receiver(tmp_path,key)) as client:
        with sqlite3.connect(tmp_path/'inbox.sqlite3') as db:
            db.execute("CREATE TRIGGER fail_inbox BEFORE INSERT ON inbox BEGIN SELECT RAISE(ABORT, 'private'); END")
        response=post(client,events(store)[0],key)
        assert response.status_code==503 and 'private' not in response.text
        with sqlite3.connect(tmp_path/'inbox.sqlite3') as db: assert db.execute('SELECT count(*) FROM inbox').fetchone()[0]==0


@pytest.mark.parametrize('change', [{'schema_version':True}, {'sequence':True}, {'occurred_at':'2026-01-01T00:00:00'}, {'event_id':'invalid'}, {'changed_fields':['private-field']}])
def test_strict_event_schema(tmp_path,activity_crm,change):
    store,tokens=activity_crm
    store.list_customers(tokens['agent'])
    key=secrets.token_urlsafe(32)
    with TestClient(receiver(tmp_path,key)) as client:
        assert post(client,{**events(store)[0],**change},key).status_code==422


def test_delivery_id_is_a_canonical_uuid(tmp_path,activity_crm):
    from helio_security.webhooks import sign
    with pytest.raises(ValueError): sign(b'{}','crm-1','x'*32,1000,'-'*36)


def test_environment_rotation_uses_explicit_key_ids(tmp_path, activity_crm, monkeypatch):
    from helio_security.webhooks import create_app, sign
    store, tokens = activity_crm
    store.list_customers(tokens['agent'])
    body = json.dumps(events(store)[0]).encode()
    monkeypatch.setenv('CRM_WEBHOOK_KEY_ID', 'new-key')
    monkeypatch.setenv('CRM_WEBHOOK_SECRET', 'n'*32)
    monkeypatch.setenv('CRM_WEBHOOK_PREVIOUS_KEY_ID', 'old-key')
    monkeypatch.setenv('CRM_WEBHOOK_PREVIOUS_SECRET', 'o'*32)
    with TestClient(create_app(tmp_path/'inbox.sqlite3')) as client:
        for key_id, secret in [('new-key', 'n'*32), ('old-key', 'o'*32)]:
            headers = sign(body, key_id, secret, int(time.time()), str(uuid.uuid4()))
            assert client.post('/webhooks/crm/activity', content=body, headers=headers).status_code in (200, 202)
    monkeypatch.setenv('CRM_WEBHOOK_PREVIOUS_KEY_ID', 'new-key')
    with pytest.raises(ValueError, match='configuration'):
        with TestClient(create_app(tmp_path/'inbox.sqlite3')):
            pass


def test_concurrent_receipt_and_sequence_conflicts(tmp_path, activity_crm):
    from concurrent.futures import ThreadPoolExecutor
    store, tokens = activity_crm
    store.list_customers(tokens['agent'])
    event, key = events(store)[0], secrets.token_urlsafe(32)
    with TestClient(receiver(tmp_path, key)) as client:
        with ThreadPoolExecutor(max_workers=4) as pool:
            statuses = list(pool.map(lambda _: post(client, event, key).status_code, range(8)))
        assert sorted(statuses) == [200]*7 + [202]
        assert post(client, {**event, 'event_id':str(uuid.uuid4())}, key).status_code == 409
        assert post(client, {**event, 'source':'another-crm'}, key).status_code == 403


def test_ambiguous_auth_headers_and_chunked_size_limit(tmp_path, activity_crm):
    from helio_security.webhooks import sign
    store, tokens = activity_crm
    store.list_customers(tokens['agent'])
    body, key = json.dumps(events(store)[0]).encode(), secrets.token_urlsafe(32)
    headers = sign(body, 'crm-1', key, int(time.time()), str(uuid.uuid4()))
    with TestClient(receiver(tmp_path, key)) as client:
        duplicate = list(headers.items()) + [('X-Awasi-Key-Id', 'crm-1')]
        assert client.post('/webhooks/crm/activity',content=body,headers=duplicate).status_code == 401
        chunks = (b'x'*8192 for _ in range(9))
        assert client.post('/webhooks/crm/activity',content=chunks,headers=headers).status_code == 413
