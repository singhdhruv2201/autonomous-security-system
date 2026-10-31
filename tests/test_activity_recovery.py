import secrets

import pytest
from fastapi.testclient import TestClient

from helio_crm.api import create_app
from helio_crm.store import CRMStore
from test_crm_activity import events


@pytest.fixture
def recovery(directory, tmp_path):
    tokens = {name: secrets.token_urlsafe(32) for name in ('admin', 'maya', 'agent', 'observer', 'recovery')}
    path = tmp_path / 'crm.sqlite3'
    with TestClient(create_app(path, tokens, directory.employees, activity_enabled=True)) as client:
        yield client, CRMStore(path, activity_enabled=True), tokens


def auth(token):
    return {'Authorization': 'Bearer ' + token}


def test_feed_is_scoped_and_does_not_recursively_export_successful_reads(recovery):
    client, store, tokens = recovery
    store.list_customers(tokens['agent'])
    before = events(store)
    headers = auth(tokens['recovery'])
    head = client.get('/activity/status', headers=headers)
    assert head.status_code == 200
    data = head.json()
    assert data['high_watermark'] == 1 and data['capture_enabled'] is True
    assert data['queue']['pending'] == 1
    page = client.get('/activity/events', headers=headers, params={'source_instance_id': data['source_instance_id'], 'through': 1})
    assert page.status_code == 200 and page.json()['items'] == before
    assert page.json()['next_after'] == 1 and page.json()['has_more'] is False
    assert events(store) == before
    assert client.get('/customers', headers=headers).status_code == 403
    assert client.get('/inventory', headers=headers).status_code == 403
    for role in ('agent', 'maya', 'observer', 'admin'):
        assert client.get('/activity/status', headers=auth(tokens[role])).status_code == 403
    assert client.get('/activity/events').status_code == 401


def test_recovery_grant_revocation_and_resource_scope_survive_restart(recovery):
    client, store, tokens = recovery
    with store.connection() as db:
        db.execute("UPDATE grants SET resource_id='another-resource' WHERE id='grant-activity-reader'")
    assert client.get('/activity/status', headers=auth(tokens['recovery'])).status_code == 403
    with store.connection() as db:
        db.execute("UPDATE grants SET resource_id='crm-activity',status='revoked' WHERE id='grant-activity-reader'")
    store.initialize([], tokens)
    assert client.get('/activity/status', headers=auth(tokens['recovery'])).status_code == 403


def test_pages_hold_the_original_watermark_and_detect_epoch_changes(recovery):
    client, store, tokens = recovery
    for _ in range(3):
        store.list_customers(tokens['agent'])
    headers = auth(tokens['recovery'])
    head = client.get('/activity/status', headers=headers).json()
    params = {'source_instance_id':head['source_instance_id'], 'through':3, 'limit':2}
    first = client.get('/activity/events', headers=headers, params=params).json()
    assert [e['sequence'] for e in first['items']] == [1, 2] and first['has_more']
    store.list_customers(tokens['agent'])
    second = client.get('/activity/events', headers=headers, params={**params, 'after':first['next_after']}).json()
    assert [e['sequence'] for e in second['items']] == [3] and not second['has_more']
    import uuid
    assert client.get('/activity/events', headers=headers, params={**params, 'source_instance_id':str(uuid.uuid4())}).status_code == 409


def test_expired_history_is_not_reported_as_empty_success(recovery):
    client, store, tokens = recovery
    store.list_customers(tokens['agent'])
    with store.connection() as db:
        db.execute('DELETE FROM activity_outbox')
        db.execute('DELETE FROM activity_events')
    headers = auth(tokens['recovery'])
    head = client.get('/activity/status', headers=headers).json()
    assert head['high_watermark'] == 1 and head['earliest_available'] == 2
    assert client.get('/activity/events', headers=headers).status_code == 410


@pytest.mark.parametrize('query', ['after=-1', 'limit=201', 'through=999999999999999999999', 'source_instance_id=invalid'])
def test_feed_rejects_invalid_cursors(recovery, query):
    client, _, tokens = recovery
    assert client.get('/activity/events?' + query, headers=auth(tokens['recovery'])).status_code == 422


def test_setup_adds_recovery_credential_without_rotating_existing_tokens(tmp_path):
    from helio_security.setup import setup
    path = tmp_path / '.env'
    setup(path)
    initial = path.read_text()
    values = dict(line.split('=', 1) for line in initial.splitlines())
    assert len(values['CRM_RECOVERY_TOKEN']) >= 32
    assert len(set(values.values())) == len(values)
    setup(path)
    assert path.read_text() == initial


def test_recovery_reader_inventory_remains_valid_for_graph_observation(recovery):
    from helio_security.models import normalize
    _, store, tokens = recovery
    inventory = store.inventory(tokens['observer'])
    normalize('crm', inventory)
    assert any(resource['id']=='crm-activity' for resource in inventory['resources'])
