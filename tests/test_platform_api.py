import secrets
from unittest.mock import Mock

import pytest
from fastapi.testclient import TestClient
from neo4j.exceptions import ServiceUnavailable

from helio_security.api import create_app
from helio_security.settings import Settings
from helio_security.platform_store import PlatformStore


@pytest.fixture
def api(tmp_path):
    settings = Settings(operator_token=secrets.token_urlsafe(32), viewer_token=secrets.token_urlsafe(32),
                        observer_token=secrets.token_urlsafe(32), db_path=tmp_path / "platform.sqlite3", sync_interval=0)
    graph = Mock(company_id="helio")
    graph.status.return_value = {"company_id": "helio", "sources": {}, "counts": {"Employee": 20}, "coverage_complete": False}
    reader = Mock()
    reader.entities.return_value = {"items": [], "total": 0, "limit": 50, "offset": 0}
    reader.entity.return_value = None
    reader.relationships.return_value = None
    reader.graph.return_value = {"nodes": [], "edges": [], "truncated": False}
    store = PlatformStore(settings.db_path)
    with TestClient(create_app(settings, graph=graph, reader=reader, store=store, start_worker=False)) as client:
        yield client, settings, store, graph, reader


def auth(settings, role="operator"):
    return {"Authorization": "Bearer " + getattr(settings, role + "_token")}


@pytest.mark.parametrize("path", ["/overview", "/entities", "/entities/missing", "/entities/missing/relationships",
                                   "/graph", "/connectors", "/policies", "/findings", "/findings/missing", "/sync-runs", "/sync-runs/missing", "/audit"])
def test_data_endpoints_require_authentication(api, path):
    client, settings, *_ = api
    assert client.get(path).status_code == 401
    assert client.get(path, headers={"Authorization": "Bearer invalid"}).status_code == 401


def test_viewer_can_read_but_cannot_schedule_or_read_operator_audit(api):
    client, settings, store, *_ = api
    for path in ("/overview", "/entities", "/graph", "/connectors", "/findings", "/sync-runs"):
        assert client.get(path, headers=auth(settings, "viewer")).status_code == 200
    assert client.post("/sync-runs", headers=auth(settings, "viewer"), json={}).status_code == 403
    assert client.get("/audit", headers=auth(settings, "viewer")).status_code == 403
    assert store.runs("helio")["total"] == 0


def test_sync_returns_durable_run_and_conflict_with_active_run(api):
    client, settings, store, *_ = api
    response = client.post("/sync-runs", headers=auth(settings), json={})
    assert response.status_code == 202
    run = response.json()
    assert run["status"] == "queued"
    assert response.headers["location"] == "/sync-runs/" + run["id"]
    assert client.get(response.headers["location"], headers=auth(settings)).json() == run
    repeated = client.post("/sync-runs", headers=auth(settings))
    assert repeated.status_code == 409 and repeated.json()["detail"]["run_id"] == run["id"]
    assert store.runs("helio")["total"] == 1
    audit = client.get("/audit", headers=auth(settings)).json()["items"]
    assert audit[0]["actor"] == "operator" and audit[0]["action"] == "sync_requested"
    assert settings.operator_token not in str(audit)


@pytest.mark.parametrize("path", ["/entities?limit=201", "/graph?limit=201", "/graph?edge_limit=501",
    "/entities?kind=Injected", "/findings?status=invalid", "/findings?offset=-1", "/sync-runs?limit=0"])
def test_queries_are_validated_and_bounded(api, path):
    client, settings, *_ = api
    assert client.get(path, headers=auth(settings)).status_code == 422


def test_clients_cannot_choose_company_or_supply_connector_urls(api):
    client, settings, store, *_ = api
    for body in ({"company_id": "other"}, {"crm_url": "http://untrusted"}):
        assert client.post("/sync-runs", headers=auth(settings), json=body).status_code == 422
    foreign, _ = store.enqueue("other", "operator", "manual", 1)
    assert client.get("/sync-runs/" + foreign["id"], headers=auth(settings)).status_code == 404
    assert client.get("/sync-runs", headers=auth(settings)).json()["total"] == 0


def test_health_and_dependency_errors_do_not_expose_details(api):
    client, settings, _, graph, _ = api
    assert client.get("/health").json() == {"status": "ok"}
    graph.driver.verify_connectivity.side_effect = ServiceUnavailable("secret-password")
    assert client.get("/health").status_code == 503
    graph.status.side_effect = ServiceUnavailable("secret-password")
    response = client.get("/connectors", headers=auth(settings))
    assert response.status_code == 503 and "secret-password" not in response.text


def test_missing_resources_are_404_and_no_remediation_route_exists(api):
    client, settings, *_ = api
    for path in ("/entities/missing", "/entities/missing/relationships", "/findings/missing", "/sync-runs/missing"):
        assert client.get(path, headers=auth(settings)).status_code == 404
    paths = client.get("/openapi.json").json()["paths"]
    assert [(path, method) for path, ops in paths.items() for method in ops if method != "get"] == [("/sync-runs", "post")]


def test_settings_require_distinct_strong_tokens_without_exposing_them():
    token = secrets.token_urlsafe(32)
    with pytest.raises(ValueError):
        Settings(operator_token=token, viewer_token=token, observer_token=token)
    with pytest.raises(ValueError):
        Settings(operator_token="short", viewer_token=token, observer_token=secrets.token_urlsafe(32))


def test_dashboard_capabilities_and_policy_describe_actual_observation(api):
    client, settings, *_ = api
    overview = client.get("/overview", headers=auth(settings, "viewer")).json()
    assert overview["observation"] == {"interval_seconds": 0, "agentic": False,
        "activity_ingestion": False, "remediation_enabled": False}
    response = client.get("/policies", headers=auth(settings, "viewer"))
    assert response.status_code == 200
    policy = response.json()["items"][0]
    assert policy["id"] == "inactive_owner_agent_access" and policy["enabled"] is True
    assert policy["sources"] == ["directory", "crm"]
    assert policy["severity"] == "high" and len(policy["conditions"]) == 4


def test_policy_modes_require_fresh_authoritative_crm_configuration(api):
    import time
    client,settings,store,*_=api
    items=client.get('/policies',headers=auth(settings)).json()['items']
    assert next(p for p in items if p['id']=='sensitive_content')['enabled'] is False
    store.set_runtime('helio','configuration',{'modes':{'sensitive_content':'enforce'}},time.time())
    items=client.get('/policies',headers=auth(settings)).json()['items']
    content=next(p for p in items if p['id']=='sensitive_content')
    assert content['mode']=='enforce' and content['enabled']
    store.set_runtime('helio','configuration',{'modes':{'sensitive_content':'enforce'}},time.time()-121)
    items=client.get('/policies',headers=auth(settings)).json()['items']
    assert next(p for p in items if p['id']=='sensitive_content')['coverage_status']=='unknown'


def test_owner_policy_coverage_reflects_publisher_outage(api):
    import time
    client,settings,store,*_=api
    store.set_runtime('helio','configuration',{'modes':{'owner_status':'enforce'}},time.time())
    store.set_runtime('helio','identity_publisher',{'status':'unavailable'},time.time())
    item=next(p for p in client.get('/policies',headers=auth(settings)).json()['items'] if p['id']=='owner_status')
    assert item['enabled'] is True  # Enforcement remains fail-closed.
    assert item['coverage_status']=='unknown'
