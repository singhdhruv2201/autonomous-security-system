import json
import pytest
from fastapi.testclient import TestClient
from helio_crm.api import create_app
from helio_crm.store import CRMStore
from test_crm_activity_api import setup
from test_crm_activity import events


def test_lifecycle_correlates_actual_response_and_operation(setup):
    directory, path, tokens = setup
    with TestClient(create_app(path, tokens, directory.employees, activity_enabled=True)) as client:
        response = client.get('/customers', headers={'Authorization': 'Bearer '+tokens['agent'], 'X-Request-ID': 'untrusted'})
    rows = events(CRMStore(path))
    assert [r['event_type'] for r in rows] == ['request.received', 'operation.completed', 'response.prepared', 'response.completed']
    assert len({r['request_id'] for r in rows}) == 1
    assert rows[0]['request_id'] != 'untrusted'
    assert rows[2]['serialized_bytes'] == len(response.content)
    assert rows[2]['result_record_count'] == 6
    assert rows[2]['scan_status'] == 'not_run'
    assert rows[-1]['completion'] == 'completed'
    assert rows[-1]['emitted_bytes'] == len(response.content)
    assert rows[-1]['actor_id'] == 'agent:sales-research'
    assert all(t not in json.dumps(rows) for t in tokens.values())


@pytest.mark.parametrize('method,path,body,status', [('get','/customers',None,401), ('patch','/customers/cust-northstar',{'notes':'private'},422), ('get','/secret-path',None,404), ('delete','/customers',None,405)])
def test_error_paths_record_sanitized_response(setup, method, path, body, status):
    directory, db_path, tokens = setup
    with TestClient(create_app(db_path, tokens, directory.employees, activity_enabled=True)) as client:
        response = client.request(method,path,json=body)
    assert response.status_code == status
    rows = events(CRMStore(db_path))
    assert rows[0]['event_type'] == 'request.received'
    assert rows[-1]['status_code'] == status
    assert rows[-1]['event_type'] == 'response.completed'
    assert 'private' not in json.dumps(rows) and 'secret-path' not in json.dumps(rows)


def test_public_routes_are_operational_only(setup):
    directory,path,tokens = setup
    with TestClient(create_app(path,tokens,directory.employees,activity_enabled=True)) as client:
        assert client.get('/health').status_code == 200
    assert events(CRMStore(path)) == []
    assert len(events(CRMStore(path),'operational_events')) == 3


def run_middleware(store, app, *, path='/customers', fail_send=False, **options):
    import asyncio
    from helio_crm.lifecycle import ActivityMiddleware
    sent=[]
    async def send(message):
        if fail_send and message['type']=='http.response.body': raise OSError('disconnected private detail')
        sent.append(message)
    async def receive(): return {'type':'http.request','body':b'','more_body':False}
    asyncio.run(ActivityMiddleware(app,store,**options)({'type':'http','path':path,'method':'GET'},receive,send))
    return sent


def test_oversized_response_is_not_released(activity_crm):
    store,_=activity_crm
    async def app(scope,receive,send):
        await send({'type':'http.response.start','status':200,'headers':[]})
        await send({'type':'http.response.body','body':b'sensitive'*100,'more_body':False})
    sent=run_middleware(store,app,max_response_bytes=4)
    assert sent[0]['status']==503
    assert b'sensitive' not in sent[-1]['body']
    assert events(store)[-1]['status_code']==503


def test_incomplete_stream_does_not_become_success(activity_crm):
    store,_=activity_crm
    async def app(scope,receive,send):
        await send({'type':'http.response.start','status':200,'headers':[]})
        await send({'type':'http.response.body','body':b'partial private','more_body':True})
    sent=run_middleware(store,app)
    assert sent[0]['status']==500
    assert b'partial private' not in sent[-1]['body']


def test_disconnect_preserves_operation_but_records_incomplete_delivery(activity_crm):
    store,tokens=activity_crm
    async def app(scope,receive,send):
        store.update_customer(tokens['maya'],'cust-northstar',1,{'notes':'committed'})
        await send({'type':'http.response.start','status':200,'headers':[]})
        await send({'type':'http.response.body','body':b'{}','more_body':False})
    with pytest.raises(OSError): run_middleware(store,app,fail_send=True)
    rows=events(store)
    assert rows[1]['outcome']=='allowed'
    assert rows[-1]['completion']=='interrupted' and rows[-1]['emitted_bytes']==0
    with store.connection() as db: assert db.execute("SELECT notes FROM customers WHERE id='cust-northstar'").fetchone()[0]=='committed'


@pytest.mark.parametrize('check,expected', [(lambda body,status:{'scan_status':'matched','release_decision':'deny'},403), (lambda body,status:{'invalid':'secret'},503)])
def test_response_hook_never_releases_denied_or_unchecked_content(activity_crm,check,expected):
    store,_=activity_crm
    async def app(scope,receive,send):
        await send({'type':'http.response.start','status':200,'headers':[]})
        await send({'type':'http.response.body','body':b'private data','more_body':False})
    sent=run_middleware(store,app,response_check=check)
    assert sent[0]['status']==expected and b'private data' not in sent[-1]['body']
    assert 'private data' not in json.dumps(events(store))


def test_unhandled_exception_is_redacted(activity_crm):
    store,_=activity_crm
    async def app(scope,receive,send): raise RuntimeError('secret detail')
    sent=run_middleware(store,app)
    assert sent[0]['status']==500
    assert 'secret detail' not in json.dumps(events(store))


def test_response_audit_failure_does_not_undo_committed_operation_or_release_data(activity_crm):
    store, tokens = activity_crm
    # request + operation fit, but response.prepared exhausts the retained stream.
    store.activity_limit = 2
    async def app(scope, receive, send):
        store.update_customer(tokens['maya'], 'cust-northstar', 1, {'notes':'committed'})
        await send({'type':'http.response.start','status':200,'headers':[]})
        await send({'type':'http.response.body','body':b'private result'})
    sent = run_middleware(store, app)
    assert sent[0]['status'] == 503 and b'private result' not in sent[-1]['body']
    assert [r['event_type'] for r in events(store)] == ['request.received','operation.completed']
    with store.connection() as db:
        assert db.execute("SELECT notes FROM customers WHERE id='cust-northstar'").fetchone()[0] == 'committed'


def test_concurrent_requests_keep_actor_and_request_correlation(setup):
    from concurrent.futures import ThreadPoolExecutor
    directory, path, tokens = setup
    with TestClient(create_app(path,tokens,directory.employees,activity_enabled=True)) as client:
        def read(actor):
            return client.get('/customers',headers={'Authorization':'Bearer '+tokens[actor]}).status_code
        with ThreadPoolExecutor(max_workers=4) as pool:
            assert list(pool.map(read, ['agent','maya']*6)) == [200]*12
    groups = {}
    for event in events(CRMStore(path)):
        groups.setdefault(event['request_id'], []).append(event)
    assert len(groups) == 12
    actors = []
    for group in groups.values():
        assert [e['event_type'] for e in group] == ['request.received','operation.completed','response.prepared','response.completed']
        assert len({e['actor_id'] for e in group[1:]}) == 1
        actors.append(group[1]['actor_id'])
    assert actors.count('agent:sales-research') == actors.count('employee:emp-maya-chen') == 6

from test_crm_activity import activity_crm
