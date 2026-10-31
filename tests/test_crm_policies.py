"""Enforcement integration: authoritative SQLite state, real API and durable evidence."""
import json
import secrets
from concurrent.futures import ThreadPoolExecutor

import pytest
from fastapi.testclient import TestClient

from helio_crm.api import create_app
from helio_crm.store import CRMError, CRMStore


@pytest.fixture
def setup(directory, tmp_path):
    tokens = {key: secrets.token_urlsafe(32) for key in ('admin','maya','agent','observer','recovery')}
    store = CRMStore(tmp_path / 'crm.db', activity_enabled=True)
    store.initialize(directory.employees(), tokens)
    return store, tokens, directory


def auth(token):
    return {'Authorization': 'Bearer '+token}


def test_customer_permission_cannot_come_from_unrelated_resource(setup):
    store,tokens,_ = setup
    with store.connection() as db:
        db.execute("UPDATE grants SET resource_id='crm-metadata' WHERE id='grant-sales-agent'")
    with pytest.raises(CRMError) as caught:
        store.list_customers(tokens['agent'])
    assert caught.value.status_code == 403
    with store.connection() as db:
        decision = dict(db.execute('SELECT * FROM policy_decisions ORDER BY id DESC').fetchone())
    assert decision['rule'] == 'resource_permissions' and decision['result'] == 'deny'
    assert decision['resource_id'] == 'customer-database'


def test_grant_for_other_app_is_not_authority(setup):
    store,tokens,_ = setup
    with store.connection() as db:
        db.execute("UPDATE grants SET app_id='other-crm' WHERE id='grant-sales-agent'")
    with pytest.raises(CRMError):
        store.list_customers(tokens['agent'])


def test_decision_is_linked_to_activity_without_sensitive_input(setup):
    store,tokens,directory = setup
    with TestClient(create_app(store.path,tokens,directory.employees,activity_enabled=True)) as client:
        response = client.patch('/customers/cust-northstar',headers=auth(tokens['agent']),
                                json={'expected_version':1,'notes':'sensitive rejected note'})
        assert response.status_code == 403
    with store.connection() as db:
        event = next(json.loads(r[0]) for r in db.execute('SELECT payload FROM activity_events') if json.loads(r[0])['event_type']=='operation.completed')
        assert event['policy_decisions'][0]['rule'] == 'resource_permissions'
        assert db.execute("SELECT version FROM customers WHERE id='cust-northstar'").fetchone()[0] == 1
        assert db.execute('SELECT count(*) FROM policy_decisions').fetchone()[0] == 1
        assert 'sensitive rejected note' not in json.dumps(event)
    from helio_security.webhooks import EVENT
    from test_crm_activity import events
    for event in events(store):
        EVENT.validate_python(event)


def projection(directory, now, version=1, **changes):
    return dict(company_id='helio',source='directory',version=version,observed_at=float(now),
                expires_at=float(now+120),complete=True,
                employees=[{'id':e['id'],'status':e['status']} for e in directory.employees()],**changes)


def test_owner_context_is_authoritative_expiring_and_restart_safe(setup):
    from helio_crm.policies import PolicyConfig
    store,tokens,directory = setup
    tokens['publisher'] = secrets.token_urlsafe(32)
    now = [1000.0]
    store = CRMStore(store.path,activity_enabled=True,policy_config=PolicyConfig(owner_mode='enforce'),clock=lambda:now[0])
    store.initialize([],tokens)
    with pytest.raises(CRMError) as caught:
        store.list_customers(tokens['agent'])
    assert caught.value.status_code == 503
    payload = projection(directory, now[0])
    with pytest.raises(CRMError):
        store.publish_identity(tokens['admin'],payload)
    store.publish_identity(tokens['publisher'],payload)
    assert len(store.list_customers(tokens['agent'])) == 6
    directory.set_employee_status('emp-maya-chen','inactive')
    now[0] += 1
    store.publish_identity(tokens['publisher'],projection(directory,now[0],2))
    for key in ('agent','maya'):
        with pytest.raises(CRMError) as caught:
            store.list_customers(tokens[key])
        assert caught.value.status_code == 403
    with pytest.raises(CRMError) as caught:
        store.publish_identity(tokens['publisher'],payload)
    assert caught.value.status_code == 409
    now[0] += 121
    store.initialize([],tokens)
    with pytest.raises(CRMError) as caught:
        store.list_customers(tokens['agent'])
    assert caught.value.status_code == 503
    # Recovery and the explicit publisher remain usable with stale directory context.
    store.inventory(tokens['observer'])
    store.activity_feed(tokens['recovery'],status_only=True)


@pytest.mark.parametrize('change',[{'company_id':'other'},{'complete':False},{'employees':[]},
                                  {'expires_at':1300.0},{'observed_at':1001.0}])
def test_projection_rejects_wrong_incomplete_future_and_overlong_context(setup,change):
    store,tokens,directory = setup
    tokens['publisher']=secrets.token_urlsafe(32)
    store = CRMStore(store.path,clock=lambda:1000.0)
    store.initialize([],tokens)
    body = {**projection(directory,1000),**change}
    with pytest.raises(CRMError):
        store.publish_identity(tokens['publisher'],body)
    with store.connection() as db:
        assert db.execute('SELECT count(*) FROM identity_projection').fetchone()[0] == 0


def test_owner_observe_mode_records_without_bypassing_permissions(setup):
    from helio_crm.policies import PolicyConfig
    store,tokens,_ = setup
    store = CRMStore(store.path,policy_config=PolicyConfig(owner_mode='observe'))
    assert len(store.list_customers(tokens['agent'])) == 6
    store.revoke_grant(tokens['admin'],'grant-sales-agent',1)
    with pytest.raises(CRMError): store.list_customers(tokens['agent'])


def test_access_lifecycle_is_versioned_and_applies_to_every_privileged_route(setup):
    store,tokens,directory = setup
    changed = store.change_access(tokens['admin'],'agent:sales-research',1,
                                  principal_status='disabled')
    assert changed['version'] == 2
    store.initialize([],tokens)
    with pytest.raises(CRMError): store.list_customers(tokens['agent'])
    with pytest.raises(CRMError) as caught:
        store.change_access(tokens['admin'],'agent:sales-research',1,principal_status='active')
    assert caught.value.status_code == 409
    with pytest.raises(CRMError):
        store.change_access(tokens['maya'],'agent:sales-research',2,principal_status='active')
    store.change_access(tokens['admin'],'agent:sales-research',2,principal_status='active')
    assert len(store.list_customers(tokens['agent'])) == 6
    store.change_access(tokens['admin'],'agent:sales-research',3,credential_status='revoked')
    store.initialize([],tokens)
    with pytest.raises(CRMError) as caught: store.list_customers(tokens['agent'])
    assert caught.value.status_code == 401


def test_expired_admin_has_no_role_based_bypass_and_recovery_uses_separate_admin(setup):
    store,tokens,_ = setup
    from helio_crm.store import token_hash
    with store.connection() as db:
        db.execute("UPDATE principals SET role='admin' WHERE id='employee:emp-maya-chen'")
    now = [1000.0]
    store = CRMStore(store.path,clock=lambda:now[0])
    store.change_access(tokens['maya'],'service:crm-admin',1,expires_at=1001.0)
    now[0] = 1001.0
    for operation in (store.list_accounts,store.list_grants,store.inventory,store.audit):
        with pytest.raises(CRMError) as caught: operation(tokens['admin'])
        assert caught.value.status_code == 401
    with pytest.raises(CRMError): store.revoke_grant(tokens['admin'],'grant-sales-agent',1)
    # An independently authorized, still-active admin performs recovery.
    store.change_access(tokens['maya'],'service:crm-admin',2,expires_at=None)
    assert store.inventory(tokens['admin'])['complete']
    store.change_access(tokens['maya'],'service:crm-admin',3,principal_status='disabled')
    with pytest.raises(CRMError) as caught: store.inventory(tokens['admin'])
    assert caught.value.status_code == 403


def limited(setup, **kwargs):
    from helio_crm.policies import PolicyConfig
    old,tokens,directory=setup
    now=[1000.0]
    return CRMStore(old.path,activity_enabled=True,policy_config=PolicyConfig(volume_mode='enforce',**kwargs),clock=lambda:now[0]),tokens,now


def test_rolling_record_budget_counts_repeated_reads_and_shared_routes(setup):
    store,tokens,now=limited(setup,record_limit=7,request_limit=100,bulk_limit=50)
    assert len(store.list_customers(tokens['agent']))==6
    store.get_customer(tokens['agent'],'cust-northstar')
    with pytest.raises(CRMError) as caught: store.get_customer(tokens['agent'],'cust-northstar')
    assert caught.value.status_code==429
    now[0]+=60
    assert len(store.list_customers(tokens['agent']))==6
    now[0]-=1
    with pytest.raises(CRMError) as caught: store.get_customer(tokens['agent'],'cust-northstar')
    assert caught.value.status_code==503


def test_atomic_request_budget_does_not_race_and_survives_restart(setup):
    store,tokens,now=limited(setup,record_limit=1000,request_limit=3)
    def read(_):
        try: store.get_customer(tokens['agent'],'cust-northstar'); return 200
        except CRMError as error: return error.status_code
    with ThreadPoolExecutor(max_workers=8) as pool: results=list(pool.map(read,range(8)))
    assert results.count(200)==3 and results.count(429)==5
    store.initialize([],tokens)
    with pytest.raises(CRMError): store.list_customers(tokens['agent'])
    assert len(store.list_customers(tokens['maya']))==6


@pytest.mark.parametrize('count,allowed',[(49,True),(50,False)])
def test_bulk_result_check_uses_actual_records_before_release(setup,count,allowed):
    store,tokens,now=limited(setup,record_limit=1000,bulk_limit=50)
    with store.connection() as db:
        db.execute('DELETE FROM customers')
        db.executemany("INSERT INTO customers VALUES (?,?,'safe@example.test','starter','active','',1)",[(str(i),str(i)) for i in range(count)])
    if allowed: assert len(store.list_customers(tokens['agent']))==49
    else:
        with pytest.raises(CRMError) as caught: store.list_customers(tokens['agent'])
        assert caught.value.status_code==403
        with store.connection() as db:
            assert db.execute('SELECT sum(reserved) FROM budget_events').fetchone()[0]==0


def test_exact_hundred_record_boundary(setup):
    store,tokens,now=limited(setup,record_limit=100,request_limit=200)
    for _ in range(100): store.get_customer(tokens['agent'],'cust-northstar')
    with pytest.raises(CRMError) as caught: store.get_customer(tokens['agent'],'cust-northstar')
    assert caught.value.status_code==429


def failures(setup):
    from helio_crm.policies import PolicyConfig
    old,tokens,_=setup
    now=[1000.0]
    return CRMStore(old.path,activity_enabled=True,policy_config=PolicyConfig(failure_mode='enforce',failure_limit=3,cooldown_seconds=30),clock=lambda:now[0]),tokens,now


def test_auth_failures_trigger_bounded_cooldown_without_spoofed_account_lockout(setup):
    store,tokens,now=failures(setup)
    for _ in range(3):
        with pytest.raises(CRMError) as caught: store.list_customers('spoofed-agent')
        assert caught.value.status_code==401
    with pytest.raises(CRMError) as caught: store.list_customers('spoofed-agent')
    assert caught.value.status_code==429
    assert len(store.list_customers(tokens['agent']))==6
    now[0]+=30
    with pytest.raises(CRMError) as caught: store.list_customers('spoofed-agent')
    assert caught.value.status_code==401


def test_authorization_failures_share_actor_budget_but_ordinary_errors_do_not(setup):
    store,tokens,now=failures(setup)
    for _ in range(4):
        with pytest.raises(CRMError) as caught: store.get_customer(tokens['agent'],'missing')
        assert caught.value.status_code==404
    for operation in (store.list_accounts,store.list_grants,store.inventory):
        with pytest.raises(CRMError) as caught: operation(tokens['agent'])
        assert caught.value.status_code==403
    store.initialize([],tokens)
    with pytest.raises(CRMError) as caught: store.list_customers(tokens['agent'])
    assert caught.value.status_code==429
    now[0]+=30
    assert len(store.list_customers(tokens['agent']))==6


def test_secret_write_and_release_are_blocked_without_leaking_evidence(setup):
    from helio_crm.policies import PolicyConfig
    old,tokens,directory=setup
    store=CRMStore(old.path,activity_enabled=True,policy_config=PolicyConfig(content_mode='enforce'))
    secret='sk_live_'+'s'*24
    with pytest.raises(CRMError) as caught:
        store.update_customer(tokens['maya'],'cust-northstar',1,{'notes':secret})
    assert caught.value.status_code==403
    with store.connection() as db:
        assert db.execute("SELECT notes,version FROM customers WHERE id='cust-northstar'").fetchone()[:]==('',1)
        db.execute("UPDATE customers SET notes=? WHERE id='cust-northstar'",(secret,))
    with TestClient(create_app(store.path,tokens,directory.employees,activity_enabled=True,policy_config=store.policy_config)) as client:
        response=client.get('/customers/cust-northstar',headers=auth(tokens['agent']))
        assert response.status_code==403 and secret not in response.text
    with store.connection() as db:
        evidence=''.join(r[0] for r in db.execute('SELECT payload FROM activity_events'))
        assert secret not in evidence
        assert 'stripe_key' in evidence
        assert '"scan_status":"matched"' in evidence


def test_pii_is_observe_only_and_oversize_is_unknown(setup):
    from helio_crm.policies import PolicyConfig
    old,tokens,_=setup
    store=CRMStore(old.path,activity_enabled=True,policy_config=PolicyConfig(content_mode='enforce',scan_max_bytes=1000))
    assert store.update_customer(tokens['maya'],'cust-northstar',1,{'notes':'SSN 123-45-6789'})['version']==2
    with store.connection() as db:
        db.execute("UPDATE customers SET notes=? WHERE id='cust-northstar'",('a'*1100,))
    with pytest.raises(CRMError) as caught: store.get_customer(tokens['agent'],'cust-northstar')
    assert caught.value.status_code==503


def test_lifecycle_activity_identifies_target_and_version_without_credentials(setup):
    from test_crm_activity import events
    store,tokens,_=setup
    store.change_access(tokens['admin'],'agent:sales-research',1,principal_status='disabled')
    event=events(store)[-1]
    assert event['record_id']=='agent:sales-research'
    assert event['changed_fields']==['principal_status']
    assert (event['before_version'],event['after_version'])==(1,2)


def test_budget_evidence_tracks_actual_enumerated_records(setup):
    from test_crm_activity import events
    store,tokens,now=limited(setup)
    result=store.list_customers(tokens['agent'])
    event=events(store)[-1]
    assert event['prepared_record_count']==6
    assert set(event['record_ids'])=={r['id'] for r in result}


def test_expired_credential_stays_expired_after_clock_rollback(setup):
    store,tokens,_=setup
    now=[1000.0];store=CRMStore(store.path,clock=lambda:now[0])
    store.change_access(tokens['admin'],'agent:sales-research',1,expires_at=1001.0)
    now[0]=1002.0
    with pytest.raises(CRMError):store.list_customers(tokens['agent'])
    now[0]=1000.0
    with pytest.raises(CRMError):store.list_customers(tokens['agent'])


def test_http_quota_denial_never_returns_customer_bytes_and_logs_denied(setup):
    from test_crm_activity import events
    store,tokens,now=limited(setup,record_limit=1)
    directory=setup[2]
    with TestClient(create_app(store.path,tokens,directory.employees,activity_enabled=True,policy_config=store.policy_config,clock=lambda:now[0])) as client:
        assert client.get('/customers/cust-northstar',headers=auth(tokens['agent'])).status_code==200
        response=client.get('/customers/cust-northstar',headers=auth(tokens['agent']))
        assert response.status_code==429 and 'Northstar' not in response.text
    operation=[e for e in events(store) if e['event_type']=='operation.completed'][-1]
    assert operation['outcome']=='denied'
    with store.connection() as db:
        assert db.execute('SELECT sum(reserved),sum(released) FROM budget_events').fetchone()[:]==(1,1)


def test_content_observe_allows_base_authorized_requests_and_labels_matches(setup):
    from helio_crm.policies import PolicyConfig
    from test_crm_activity import events
    old,tokens,_=setup
    store=CRMStore(old.path,activity_enabled=True,policy_config=PolicyConfig(content_mode='observe'))
    store.update_customer(tokens['maya'],'cust-northstar',1,{'notes':'sk_live_'+'x'*24})
    decision=next(d for d in events(store)[-1]['policy_decisions'] if d['rule']=='sensitive_content')
    assert decision['result']=='would_deny' and decision['mode']=='observe'
    with pytest.raises(CRMError): store.update_customer(tokens['agent'],'cust-northstar',2,{'notes':'safe'})


def test_new_api_surfaces_authorize_and_preserve_access_state(setup):
    store,tokens,directory=setup
    tokens['publisher']=secrets.token_urlsafe(32)
    now=1000.0
    with TestClient(create_app(store.path,tokens,directory.employees,activity_enabled=True,clock=lambda:now)) as client:
        body=projection(directory,now)
        assert client.post('/identity-projection',json=body).status_code==401
        assert client.post('/identity-projection',headers=auth(tokens['observer']),json=body).status_code==403
        assert client.post('/identity-projection',headers=auth(tokens['publisher']),json=body).status_code==200
        path='/principals/agent:sales-research/access'
        change={'expected_version':1,'principal_status':'disabled'}
        assert client.patch(path,headers=auth(tokens['maya']),json=change).status_code==403
        assert client.patch(path,headers=auth(tokens['admin']),json=change).status_code==200
        assert client.get('/customers',headers=auth(tokens['agent'])).status_code==403
    from helio_security.webhooks import EVENT
    from test_crm_activity import events
    for event in events(store): EVENT.validate_python(event)


def test_lock_wait_is_not_mistaken_for_clock_regression(setup):
    import threading
    from contextlib import contextmanager
    store,tokens,_=limited(setup,record_limit=1000)
    ticks=iter([1000.0+i/10 for i in range(20)])
    store.clock=lambda:next(ticks)
    original=store._operation_connection
    waiting,fast_done=threading.Event(),threading.Event()
    @contextmanager
    def delayed():
        if threading.current_thread().name=='slow':
            waiting.set(); assert fast_done.wait(3)
        with original() as db: yield db
    store._operation_connection=delayed
    errors=[]
    def slow():
        try: store.get_customer(tokens['agent'],'cust-northstar')
        except Exception as error: errors.append(error)
    thread=threading.Thread(target=slow,name='slow');thread.start()
    assert waiting.wait(3)
    try: store.get_customer(tokens['agent'],'cust-northstar')
    finally: fast_done.set()
    thread.join(3)
    assert not errors


def test_authorized_inventory_exposes_lifecycle_versions_for_safe_updates(setup):
    store,tokens,_=setup
    store.change_access(tokens['admin'],'agent:sales-research',1,principal_status='disabled')
    account=next(a for a in store.list_accounts(tokens['observer']) if a['id']=='agent:sales-research')
    assert account['principal_status']=='disabled' and account['access_version']==2
    assert account['credential_status']=='active' and account['credential_expires_at'] is None
    with pytest.raises(CRMError):store.list_accounts(tokens['agent'])


def test_new_policy_routes_have_named_redacted_http_evidence(setup):
    from test_crm_activity import events
    from helio_security.webhooks import EVENT
    store,tokens,directory=setup
    tokens['publisher']=secrets.token_urlsafe(32)
    with TestClient(create_app(store.path,tokens,directory.employees,activity_enabled=True,clock=lambda:1000.0)) as client:
        assert client.post('/identity-projection',headers=auth(tokens['publisher']),json=projection(directory,1000)).status_code==200
        assert client.patch('/principals/agent:sales-research/access',headers=auth(tokens['admin']),json={'expected_version':1,'principal_status':'disabled'}).status_code==200
    operational=events(store,'operational_events')
    security=events(store)
    assert [e['route'] for e in operational if e['event_type']!='operation.completed']==['/identity-projection']*3
    assert [e['route'] for e in security if e['event_type']!='operation.completed']==['/principals/{id}/access']*3
    for event in security: EVENT.validate_python(event)


@pytest.mark.parametrize('failure',['disconnect','response_audit'])
def test_prepared_disclosures_stay_charged_when_delivery_fails(setup,failure):
    from test_crm_lifecycle import run_middleware
    store,tokens,now=limited(setup,record_limit=1)
    async def app(scope,receive,send):
        result=store.get_customer(tokens['agent'],'cust-northstar')
        await send({'type':'http.response.start','status':200,'headers':[]})
        await send({'type':'http.response.body','body':json.dumps(result).encode()})
    if failure=='disconnect':
        with pytest.raises(OSError): run_middleware(store,app,fail_send=True)
    else:
        store.activity_limit=2
        sent=run_middleware(store,app)
        assert sent[0]['status']==503 and b'Northstar' not in sent[-1]['body']
        store.activity_limit=10000
    with store.connection() as db:
        assert db.execute('SELECT sum(reserved),sum(released) FROM budget_events').fetchone()[:]==(1,0)
    with pytest.raises(CRMError) as caught: store.get_customer(tokens['agent'],'cust-northstar')
    assert caught.value.status_code==429


def test_observe_volume_and_failure_policies_keep_base_permissions_enforced(setup):
    from helio_crm.policies import PolicyConfig
    from test_crm_activity import events
    old,tokens,_=setup
    store=CRMStore(old.path,activity_enabled=True,policy_config=PolicyConfig(volume_mode='observe',record_limit=1,failure_mode='observe',failure_limit=2))
    for _ in range(3):
        with pytest.raises(CRMError) as caught: store.list_grants(tokens['agent'])
        assert caught.value.status_code==403
    assert len(store.list_customers(tokens['agent']))==6
    decisions=[d for e in events(store) for d in e.get('policy_decisions',[])]
    assert any(d['rule']=='authentication_failures' and d['result']=='would_deny' for d in decisions)
    assert any(d['rule']=='extraction_limits' and d['result']=='would_deny' for d in decisions)
