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
    assert [(path, method) for path, ops in paths.items() for method in ops if method != "get"] == [
        ("/cases/{case_id}/decisions", "post"),
        ("/sync-runs", "post"),
    ]


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
    assert "policy_runtime" not in overview
    assert len(response.json()["items"]) == 1
    policy = response.json()["items"][0]
    assert policy["id"] == "inactive_owner_agent_access" and policy["enabled"] is True
    assert policy["sources"] == ["directory", "crm"]
    assert policy["severity"] == "high" and len(policy["conditions"]) == 4



@pytest.mark.parametrize("complete", [False, True])
def test_policy_coverage_comes_from_graph_not_retired_runtime(api, complete):
    client, settings, store, graph, _ = api
    store.set_runtime("helio", "configuration", {"modes": {"sensitive_content": "enforce"}}, 100)
    store.set_runtime("helio", "identity_publisher", {"status": "unavailable"}, 100)
    graph.status.return_value["coverage_complete"] = complete
    result = client.get("/policies", headers=auth(settings)).json()
    assert set(result) == {"company_id", "items"}
    assert [p["id"] for p in result["items"]] == ["inactive_owner_agent_access"]
    assert result["items"][0]["enabled"] is True
    assert result["items"][0]["coverage_status"] == ("current" if complete else "unknown")


def test_cases_api_and_overview_heartbeat(api):
    client, settings, store, *_ = api
    import time
    # Check overview without heartbeat
    ov1 = client.get("/overview", headers=auth(settings, "viewer")).json()
    assert ov1["observation"]["agentic"] is False
    assert ov1["observation"]["remediation_enabled"] is False

    # Check overview with fresh heartbeat
    store.set_runtime("helio", "agent_worker", {"status": "ok"}, time.time())
    ov2 = client.get("/overview", headers=auth(settings, "viewer")).json()
    assert ov2["observation"]["agentic"] is True
    assert ov2["observation"]["remediation_enabled"] is True

    # Check cases list initially empty
    resp = client.get("/cases", headers=auth(settings, "viewer"))
    assert resp.status_code == 200
    assert resp.json()["total"] == 0

    # Open a case directly via case_store
    client.app.state.services.case_store.open_case("helio", "finding-1", route="fix_then_ask", now=time.time())
    cases = client.get("/cases", headers=auth(settings, "viewer")).json()
    assert cases["total"] == 1
    case_id = cases["items"][0]["id"]

    # Read case full
    detail = client.get(f"/cases/{case_id}", headers=auth(settings, "viewer"))
    assert detail.status_code == 200
    assert detail.json()["id"] == case_id
    assert "events" in detail.json()
    assert "actions" in detail.json()

    # Viewer cannot post decision
    dec_resp = client.post(f"/cases/{case_id}/decisions", headers=auth(settings, "viewer"), json={"key": "k1", "choice": "retire"})
    assert dec_resp.status_code == 403

    # Operator can post decision
    dec_resp = client.post(f"/cases/{case_id}/decisions", headers=auth(settings, "operator"), json={"key": "k1", "choice": "retire"})
    assert dec_resp.status_code == 200
    assert dec_resp.json()["status"] == "recorded"
