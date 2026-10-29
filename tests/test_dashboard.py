import secrets
from unittest.mock import Mock

import pytest
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, RedirectResponse, Response
from fastapi.testclient import TestClient

from test_graph import serve
from helio_dashboard.api import create_app
from helio_dashboard.gateway import Gateway, UpstreamError


@pytest.fixture
def upstream():
    app = FastAPI()
    seen = []

    @app.api_route("/{path:path}", methods=["GET", "POST"])
    def endpoint(path: str, request: Request):
        seen.append((request.method, path, request.headers.get("authorization")))
        if path == "overview":
            return {"company_id": "helio", "coverage_complete": True, "counts": {"Employee": 20}}
        if path == "graph":
            return {"nodes": [], "edges": [], "truncated": False}
        if path == "denied":
            return JSONResponse({"detail": "secret error body"}, status_code=403)
        if path == "redirect":
            return RedirectResponse("/overview", status_code=302)
        if path == "conflict":
            return JSONResponse({"detail": {"run_id": "00000000-0000-4000-8000-000000000000",
                                           "private": "must not pass through"}}, status_code=409)
        if path == "bad-conflict":
            return JSONResponse({"detail": {"run_id": "not-a-uuid"}}, status_code=409)
        if path == "invalid-json":
            return Response("not JSON", media_type="application/json")
        if path == "invalid-shape":
            return JSONResponse(["unexpected list"])
        if path == "oversized":
            return Response(b" " * (2 * 1024 * 1024 + 1))
        if path == "sync-runs" and request.method == "POST":
            return JSONResponse({"id": "test-run", "status": "queued"}, status_code=202)
        return {"items": [], "total": 0}

    with serve(app) as url:
        yield url, seen


def test_gateway_uses_real_http_and_keeps_service_tokens_server_side(upstream):
    url, seen = upstream
    viewer, operator = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
    gateway = Gateway(url, viewer, operator)
    state = gateway.state()
    assert state["overview"]["counts"]["Employee"] == 20
    assert state["errors"] == {}
    assert len(seen) == 7
    assert all(row[2] == "Bearer " + viewer for row in seen)
    assert viewer not in str(state) and operator not in str(state)
    status, result = gateway.request("/sync-runs", method="POST")
    assert status == 202 and result["status"] == "queued"
    assert seen[-1][2] == "Bearer " + operator


def test_gateway_refuses_redirect_and_redacts_upstream_errors(upstream):
    url, seen = upstream
    gateway = Gateway(url, "viewer", "operator")
    with pytest.raises(UpstreamError) as caught:
        gateway.request("/denied")
    assert str(caught.value) == "upstream_403"
    with pytest.raises(UpstreamError, match="upstream_302"):
        gateway.request("/redirect")
    assert len(seen) == 2


@pytest.mark.parametrize("path,reason", [("invalid-json", "invalid_payload"),
    ("invalid-shape", "invalid_payload"), ("oversized", "oversized"),
    ("bad-conflict", "upstream_409")])
def test_gateway_bounds_and_validates_upstream_payloads(upstream, path, reason):
    gateway = Gateway(upstream[0], "viewer", "operator")
    with pytest.raises(UpstreamError, match=reason):
        gateway.request("/" + path)


def test_gateway_preserves_only_safe_running_job_conflict(upstream):
    gateway = Gateway(upstream[0], "viewer", "operator")
    status, body = gateway.request("/conflict", method="POST")
    assert status == 409
    assert body == {"detail": {"code": "sync_in_progress",
                              "run_id": "00000000-0000-4000-8000-000000000000"}}


@pytest.mark.parametrize("viewer,operator", [("", ""), ("short", "x" * 40), ("x" * 40, "x" * 40)])
def test_dashboard_refuses_missing_short_or_shared_credentials(monkeypatch, viewer, operator):
    monkeypatch.setenv("SECURITY_VIEWER_TOKEN", viewer)
    monkeypatch.setenv("SECURITY_OPERATOR_TOKEN", operator)
    with pytest.raises(ValueError, match="credentials missing or invalid"):
        Gateway.from_environment()


def test_partial_data_is_explicit_and_not_a_false_empty_success(upstream, monkeypatch):
    gateway = Gateway(upstream[0], "viewer", "operator")
    original = gateway.request
    def request(path, **kwargs):
        if path.startswith("/findings"):
            raise UpstreamError("upstream_503")
        return original(path, **kwargs)
    monkeypatch.setattr(gateway, "request", request)
    state = gateway.state()
    assert state["findings"] is None and state["errors"] == {"findings": "upstream_503"}


@pytest.fixture
def dashboard():
    gateway = Mock()
    gateway.state.return_value = {"overview": {"company_id": "helio"}, "errors": {}}
    gateway.request.return_value = (202, {"id": "test-run", "status": "queued"})
    with TestClient(create_app(gateway)) as client:
        yield client, gateway


def test_dashboard_serves_local_assets_and_rejects_foreign_hosts(dashboard):
    client, gateway = dashboard
    page = client.get("/")
    assert page.status_code == 200 and "awasi" in page.text
    assert client.get("/assets/app.mjs").status_code == 200
    assert client.get("/assets/styles.css").status_code == 200
    assert "default-src 'self'" in page.headers["content-security-policy"]
    assert client.get("/api/state").json()["overview"]["company_id"] == "helio"
    assert client.get("/api/state", headers={"host": "evil.example"}).status_code == 400


def test_refresh_requires_same_origin_and_cannot_proxy_arbitrary_actions(dashboard):
    client, gateway = dashboard
    assert client.post("/api/sync-runs").status_code == 403
    assert client.post("/api/sync-runs", headers={"origin": "https://evil.example", "x-awasi-request": "dashboard"}).status_code == 403
    headers = {"origin": "http://testserver", "x-awasi-request": "dashboard"}
    assert client.post("/api/sync-runs", headers=headers, json={"url": "http://evil"}).status_code == 422
    assert client.post("/api/sync-runs", headers=headers, json={}).status_code == 202
    gateway.request.assert_called_once_with("/sync-runs", method="POST")
    assert client.post("/api/grants/revoke", headers=headers).status_code == 404
    assert client.get("/api/sync-runs/not-a-uuid").status_code == 422


def test_total_backend_failure_is_visible_and_never_exposes_exception(dashboard):
    client, gateway = dashboard
    gateway.state.return_value = {"overview": None, "errors": {"overview": "upstream_503"}}
    assert client.get("/api/state").status_code == 503
    gateway.request.side_effect = UpstreamError("unavailable")
    response = client.get("/api/findings/00000000-0000-4000-8000-000000000000")
    assert response.status_code == 503 and response.json() == {"detail": "Security API unavailable"}
