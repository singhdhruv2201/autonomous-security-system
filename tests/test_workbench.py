"""Tests for the Helio Workbench service (Phase 4).

TDD: tests written first. Validates:
- The public link returns data only while public (404 when company/private).
- A paused agent refuses to run (returns 409).
- A disabled destination receives nothing.
- Stale versions return 409 on PATCH /apps, /agents, and /destinations.
- Every send and view is logged in events.
- Observer and action tokens cannot do each other's operations.
- Inventory counts (anonymous_views_1h, records_sent_1h, records_sent_total) match the event log.
"""
import json
import secrets
from unittest.mock import patch, MagicMock
import httpx
import pytest
from fastapi.testclient import TestClient

from helio_workbench.store import WorkbenchStore
from helio_workbench.api import create_app


SAMPLE_CUSTOMERS = [
    {"id": "cust-northstar", "name": "Northstar Labs", "contact_email": "contact@northstar.example", "plan": "growth", "subscription_status": "active"},
    {"id": "cust-orchard", "name": "Orchard Studio", "contact_email": "contact@orchard.example", "plan": "starter", "subscription_status": "trial"},
    {"id": "cust-bluebird", "name": "Bluebird Logistics", "contact_email": "contact@bluebird.example", "plan": "enterprise", "subscription_status": "active"},
    {"id": "cust-cedar", "name": "Cedar Analytics", "contact_email": "contact@cedar.example", "plan": "growth", "subscription_status": "active"},
    {"id": "cust-harbor", "name": "Harbor Design", "contact_email": "contact@harbor.example", "plan": "starter", "subscription_status": "cancelled"},
    {"id": "cust-summit", "name": "Summit Research", "contact_email": "contact@summit.example", "plan": "growth", "subscription_status": "trial"},
]


@pytest.fixture
def workbench_setup(tmp_path):
    db_path = tmp_path / "workbench.sqlite3"
    tokens = {
        "observer": secrets.token_urlsafe(32),
        "action": secrets.token_urlsafe(32),
        "user": secrets.token_urlsafe(32),
    }
    crm_tokens = {
        "service:renewal-tracker": "tok-renewal-tracker-12345678901234567890",
        "service:support-lookup": "tok-support-lookup-12345678901234567890",
        "agent:lead-enrichment": "tok-lead-enrichment-12345678901234567890",
        "agent:renewal-digest": "tok-renewal-digest-12345678901234567890",
    }
    store = WorkbenchStore(db_path)
    store.initialize()

    app = create_app(
        store=store,
        observer_token=tokens["observer"],
        action_token=tokens["action"],
        user_token=tokens["user"],
        crm_url="http://crm:8001",
        crm_tokens=crm_tokens,
    )
    client = TestClient(app)
    return store, client, tokens, crm_tokens


def auth_header(token):
    return {"Authorization": f"Bearer {token}"}


def test_public_link_returns_data_only_while_public(workbench_setup):
    store, client, tokens, _ = workbench_setup

    # 1. By default, app-renewal-tracker has visibility='company' -> GET /public/renewals returns 404
    resp = client.get("/public/renewals")
    assert resp.status_code == 404

    # Internal user can view company app
    with patch("httpx.get", return_value=httpx.Response(200, json=SAMPLE_CUSTOMERS)):
        resp_user = client.get("/apps/renewals", headers=auth_header(tokens["user"]))
        assert resp_user.status_code == 200
        assert len(resp_user.json()) == 6

    # 2. Publish the app via action token
    patch_resp = client.patch(
        "/apps/app-renewal-tracker",
        headers=auth_header(tokens["action"]),
        json={"expected_version": 1, "visibility": "public"},
    )
    assert patch_resp.status_code == 200
    assert patch_resp.json()["visibility"] == "public"
    assert patch_resp.json()["version"] == 2

    # 3. Public link now returns data without authentication
    with patch("httpx.get", return_value=httpx.Response(200, json=SAMPLE_CUSTOMERS)):
        resp_public = client.get("/public/renewals")
        assert resp_public.status_code == 200
        data = resp_public.json()
        assert len(data) == 6
        assert data[0]["contact_email"] == "contact@northstar.example"

    # 4. Restrict back to company
    client.patch(
        "/apps/app-renewal-tracker",
        headers=auth_header(tokens["action"]),
        json={"expected_version": 2, "visibility": "company"},
    )
    resp = client.get("/public/renewals")
    assert resp.status_code == 404


def test_paused_agent_refuses_to_run(workbench_setup):
    store, client, tokens, _ = workbench_setup

    # 1. Pause the agent via action token
    resp = client.patch(
        "/agents/agent-lead-enrichment",
        headers=auth_header(tokens["action"]),
        json={"expected_version": 1, "status": "paused"},
    )
    assert resp.status_code == 200
    assert resp.json()["status"] == "paused"
    assert resp.json()["version"] == 2

    # 2. Attempt to run paused agent returns 409
    run_resp = client.post("/agents/agent-lead-enrichment/run", headers=auth_header(tokens["user"]))
    assert run_resp.status_code == 409
    assert "paused" in run_resp.json()["detail"].lower()


def test_disabled_destination_receives_nothing(workbench_setup):
    store, client, tokens, _ = workbench_setup

    # Control agent agent-renewal-digest has partner-api enabled
    # Disable the destination via action token
    resp = client.patch(
        "/agents/agent-renewal-digest/destinations/partner-api",
        headers=auth_header(tokens["action"]),
        json={"expected_version": 1, "enabled": False},
    )
    assert resp.status_code == 200
    assert resp.json()["enabled"] is False
    assert resp.json()["version"] == 2

    # Run agent: should NOT post to partner-api because destination is disabled
    with patch("httpx.get", return_value=httpx.Response(200, json=SAMPLE_CUSTOMERS)), \
         patch("httpx.post") as mock_post:
        run_resp = client.post("/agents/agent-renewal-digest/run", headers=auth_header(tokens["user"]))
        assert run_resp.status_code == 200
        assert run_resp.json()["destinations_count"] == 0
        mock_post.assert_not_called()


def test_stale_versions_return_409(workbench_setup):
    store, client, tokens, _ = workbench_setup

    # Stale version on app
    resp = client.patch(
        "/apps/app-renewal-tracker",
        headers=auth_header(tokens["action"]),
        json={"expected_version": 99, "visibility": "public"},
    )
    assert resp.status_code == 409

    # Stale version on agent
    resp = client.patch(
        "/agents/agent-lead-enrichment",
        headers=auth_header(tokens["action"]),
        json={"expected_version": 99, "status": "paused"},
    )
    assert resp.status_code == 409

    # Stale version on destination
    resp = client.patch(
        "/agents/agent-renewal-digest/destinations/partner-api",
        headers=auth_header(tokens["action"]),
        json={"expected_version": 99, "enabled": False},
    )
    assert resp.status_code == 409


@pytest.mark.parametrize("kind", ["app", "agent", "destination"])
def test_concurrent_updates_reject_one_stale_writer(tmp_path, kind):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier
    from helio_workbench.store import ConflictError

    store = WorkbenchStore(tmp_path / f"race_{kind}.sqlite3")
    original = store.connection
    barrier = Barrier(2)

    def connection():
        db = original()
        def trace(sql):
            if sql.startswith("UPDATE "):
                barrier.wait(timeout=3)
        db.set_trace_callback(trace)
        return db

    store.connection = connection

    def update(value):
        try:
            if kind == "app":
                store.update_app("app-renewal-tracker", 1, visibility=value)
            elif kind == "agent":
                store.update_agent("agent-lead-enrichment", 1, status=value)
            else:
                store.update_destination("agent-renewal-digest", "partner-api", 1, enabled=value)
            return "ok"
        except ConflictError:
            return "conflict"

    choices = {"app": ["public", "private"], "agent": ["paused", "active"], "destination": [False, True]}
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(update, choices[kind]))
    assert sorted(results) == ["conflict", "ok"], results


def test_every_send_and_view_is_logged_and_inventory_matches(workbench_setup):
    store, client, tokens, _ = workbench_setup

    # Make app public and view it
    client.patch("/apps/app-renewal-tracker", headers=auth_header(tokens["action"]), json={"expected_version": 1, "visibility": "public"})
    with patch("httpx.get", return_value=httpx.Response(200, json=SAMPLE_CUSTOMERS)):
        client.get("/public/renewals")
        client.get("/public/renewals")

    # Add destination to agent-lead-enrichment and run it
    store.add_destination("agent-lead-enrichment", "enrich-leads", "http://enrich-leads:8005/collect", enabled=True)
    with patch("httpx.get", return_value=httpx.Response(200, json=SAMPLE_CUSTOMERS)), \
         patch("httpx.post", return_value=httpx.Response(200, json={"status": "received"})) as mock_post:
        run_resp = client.post("/agents/agent-lead-enrichment/run", headers=auth_header(tokens["user"]))
        assert run_resp.status_code == 200
        assert run_resp.json()["records_sent"] == 6

    # Fetch events via observer token
    events_resp = client.get("/events", headers=auth_header(tokens["observer"]))
    assert events_resp.status_code == 200
    events = events_resp.json()["items"]
    kinds = [e["kind"] for e in events]
    assert "admin.change" in kinds
    assert "app.view" in kinds
    assert "agent.send" in kinds

    # Verify inventory counts match
    inv_resp = client.get("/inventory", headers=auth_header(tokens["observer"]))
    assert inv_resp.status_code == 200
    inv = inv_resp.json()
    assert inv["schema_version"] == "1"
    assert inv["complete"] is True

    # App anonymous views
    app_info = next(a for a in inv["apps"] if a["id"] == "app-renewal-tracker")
    assert app_info["anonymous_views_1h"] == 2
    assert app_info["last_anonymous_view_at"] is not None

    # Agent records sent
    agent_info = next(a for a in inv["agents"] if a["id"] == "agent-lead-enrichment")
    dest_info = next(d for d in agent_info["destinations"] if d["host"] == "enrich-leads")
    assert dest_info["records_sent_1h"] == 6
    assert dest_info["records_sent_total"] == 6
    assert dest_info["last_sent_at"] is not None


def test_observer_and_action_tokens_cannot_do_each_other_operations(workbench_setup):
    store, client, tokens, _ = workbench_setup

    # Observer token cannot mutate apps
    resp = client.patch(
        "/apps/app-renewal-tracker",
        headers=auth_header(tokens["observer"]),
        json={"expected_version": 1, "visibility": "public"},
    )
    assert resp.status_code == 403

    # Observer token cannot mutate agents
    resp = client.patch(
        "/agents/agent-lead-enrichment",
        headers=auth_header(tokens["observer"]),
        json={"expected_version": 1, "status": "paused"},
    )
    assert resp.status_code == 403

    # Action token cannot read inventory
    resp = client.get("/inventory", headers=auth_header(tokens["action"]))
    assert resp.status_code == 403

    # Action token cannot read events
    resp = client.get("/events", headers=auth_header(tokens["action"]))
    assert resp.status_code == 403


def test_workbench_control_cli_actions(tmp_path, monkeypatch):
    import sys
    from helio_workbench.control import main as control_main
    from helio_workbench.runner import main as runner_main

    db_path = tmp_path / "wb_cli.sqlite3"
    store = WorkbenchStore(db_path)
    store.initialize()

    tokens = {
        "observer": secrets.token_urlsafe(32),
        "action": secrets.token_urlsafe(32),
        "user": secrets.token_urlsafe(32),
    }
    app = create_app(
        store=store,
        observer_token=tokens["observer"],
        action_token=tokens["action"],
        user_token=tokens["user"],
        crm_url="http://crm:8001",
        crm_tokens={
            "service:renewal-tracker": "tok-renewal-tracker-12345678901234567890",
            "agent:lead-enrichment": "tok-lead-enrichment-12345678901234567890",
        },
    )

    class MockTransport(httpx.BaseTransport):
        def handle_request(self, request):
            with TestClient(app) as test_client:
                # Dispatch request into TestClient
                headers = dict(request.headers)
                headers.pop("host", None)
                content = request.read()
                resp = test_client.request(
                    method=request.method,
                    url=str(request.url),
                    headers=headers,
                    content=content,
                )
                return httpx.Response(
                    status_code=resp.status_code,
                    headers=resp.headers,
                    content=resp.content,
                )

    # Mock urllib.request.urlopen in control and runner using TestClient
    def mock_urlopen(req, timeout=5):
        import io
        import urllib.response
        with TestClient(app) as tc:
            url_str = req.full_url if hasattr(req, "full_url") else str(req)
            method = req.get_method() if hasattr(req, "get_method") else "GET"
            headers = dict(req.header_items()) if hasattr(req, "header_items") else {}
            data = req.data if hasattr(req, "data") else None
            # Extract path
            from urllib.parse import urlparse
            parsed = urlparse(url_str)
            path = parsed.path
            if parsed.query:
                path += "?" + parsed.query
            resp = tc.request(method, path, headers=headers, content=data)
            fp = io.BytesIO(resp.content)
            res = urllib.response.addinfourl(fp, resp.headers, url_str, resp.status_code)
            res.code = resp.status_code
            return res

    monkeypatch.setattr("urllib.request.urlopen", mock_urlopen)
    monkeypatch.setenv("WORKBENCH_OBSERVER_TOKEN", tokens["observer"])
    monkeypatch.setenv("WORKBENCH_ACTION_TOKEN", tokens["action"])
    monkeypatch.setenv("WORKBENCH_USER_TOKEN", tokens["user"])

    # 1. Publish app
    monkeypatch.setattr(sys, "argv", ["control.py", "publish-app"])
    control_main()
    assert store.get_app_by_id("app-renewal-tracker")["visibility"] == "public"

    # 2. Restrict app
    monkeypatch.setattr(sys, "argv", ["control.py", "restrict-app"])
    control_main()
    assert store.get_app_by_id("app-renewal-tracker")["visibility"] == "company"

    # 3. Connect enrichment
    monkeypatch.setattr(sys, "argv", ["control.py", "connect-enrichment"])
    control_main()
    dest = store.get_destination("agent-lead-enrichment", "enrich-leads")
    assert dest is not None
    assert dest["enabled"] is True

    # 4. Status / inventory
    monkeypatch.setattr(sys, "argv", ["control.py", "status"])
    control_main()

    # 5. Outsider view after publishing
    monkeypatch.setattr(sys, "argv", ["control.py", "publish-app"])
    control_main()
    with patch("httpx.get", return_value=httpx.Response(200, json=SAMPLE_CUSTOMERS)):
        monkeypatch.setattr(sys, "argv", ["control.py", "outsider-view"])
        control_main()

    # 6. Runner runs agent-lead-enrichment
    with patch("httpx.get", return_value=httpx.Response(200, json=SAMPLE_CUSTOMERS)), \
         patch("httpx.post", return_value=httpx.Response(200, json={"status": "received"})) as mock_post:
        monkeypatch.setattr(sys, "argv", ["runner.py", "agent-lead-enrichment"])
        runner_main()
        mock_post.assert_called_once()


def test_add_cannot_silently_replace_disabled_connection(workbench_setup):
    store, client, tokens, _ = workbench_setup
    path = "/agents/agent-renewal-digest/destinations/partner-api"
    # Disable the destination
    assert client.patch(path, headers=auth_header(tokens["action"]), json={"expected_version": 1, "enabled": False}).status_code == 200

    # Attempting to recreate the destination via POST must return 409 and not change state
    resp = client.post(
        "/agents/agent-renewal-digest/destinations",
        headers=auth_header(tokens["user"]),
        json={"host": "partner-api", "url": "http://partner-api:8005/collect", "enabled": True},
    )
    saved = store.get_destination("agent-renewal-digest", "partner-api")
    assert resp.status_code == 409
    assert saved["version"] == 2
    assert saved["enabled"] is False


def test_destination_identity_and_url_validation(workbench_setup):
    store, client, tokens, _ = workbench_setup

    # Mismatched declared host and URL host -> 422
    resp = client.post(
        "/agents/agent-lead-enrichment/destinations",
        headers=auth_header(tokens["user"]),
        json={"host": "partner-api", "url": "http://other-host:8005/collect"},
    )
    assert resp.status_code == 422

    # Malformed / unsupported scheme -> 422
    resp = client.post(
        "/agents/agent-lead-enrichment/destinations",
        headers=auth_header(tokens["user"]),
        json={"host": "partner-api", "url": "ftp://partner-api:8005/collect"},
    )
    assert resp.status_code == 422

    # Embedded credentials in URL -> 422
    resp = client.post(
        "/agents/agent-lead-enrichment/destinations",
        headers=auth_header(tokens["user"]),
        json={"host": "partner-api", "url": "http://user:secret@partner-api:8005/collect"},
    )
    assert resp.status_code == 422

    # Valid destination with matching host -> 200
    resp = client.post(
        "/agents/agent-lead-enrichment/destinations",
        headers=auth_header(tokens["user"]),
        json={"host": "enrich-leads", "url": "http://enrich-leads:8005/collect", "enabled": True},
    )
    assert resp.status_code == 200
    assert resp.json()["host"] == "enrich-leads"

    # PATCH destination URL to a different host -> 422
    resp = client.patch(
        "/agents/agent-lead-enrichment/destinations/enrich-leads",
        headers=auth_header(tokens["action"]),
        json={"expected_version": 1, "url": "http://hijacked-host:8005/collect"},
    )
    assert resp.status_code == 422


def test_capability_separation_matrix(workbench_setup):
    store, client, tokens, crm_tokens = workbench_setup
    crm_token = next(iter(crm_tokens.values()))

    # Missing authentication -> 401
    assert client.get("/inventory").status_code == 401
    assert client.get("/events").status_code == 401
    assert client.get("/apps/renewals").status_code == 401
    assert client.patch("/apps/app-renewal-tracker", json={"expected_version": 1, "visibility": "public"}).status_code == 401
    assert client.post("/agents/agent-lead-enrichment/run").status_code == 401

    # Observer credential -> only inventory and events
    assert client.get("/inventory", headers=auth_header(tokens["observer"])).status_code == 200
    assert client.get("/events", headers=auth_header(tokens["observer"])).status_code == 200
    assert client.get("/apps/renewals", headers=auth_header(tokens["observer"])).status_code == 403
    assert client.patch("/apps/app-renewal-tracker", headers=auth_header(tokens["observer"]), json={"expected_version": 1, "visibility": "public"}).status_code == 403
    assert client.post("/agents/agent-lead-enrichment/run", headers=auth_header(tokens["observer"])).status_code == 403

    # Action credential -> administrative mutations only
    assert client.get("/inventory", headers=auth_header(tokens["action"])).status_code == 403
    assert client.get("/events", headers=auth_header(tokens["action"])).status_code == 403
    assert client.get("/apps/renewals", headers=auth_header(tokens["action"])).status_code == 403
    assert client.post("/agents/agent-lead-enrichment/run", headers=auth_header(tokens["action"])).status_code == 403

    # User credential -> user views, destination creation, agent run
    assert client.get("/inventory", headers=auth_header(tokens["user"])).status_code == 403
    assert client.get("/events", headers=auth_header(tokens["user"])).status_code == 403
    assert client.patch("/apps/app-renewal-tracker", headers=auth_header(tokens["user"]), json={"expected_version": 1, "visibility": "public"}).status_code == 403

    # Outbound CRM principal tokens -> cannot act as workbench user, action, or observer
    assert client.get("/inventory", headers=auth_header(crm_token)).status_code == 403
    assert client.get("/apps/renewals", headers=auth_header(crm_token)).status_code == 403
    assert client.patch("/apps/app-renewal-tracker", headers=auth_header(crm_token), json={"expected_version": 1, "visibility": "public"}).status_code == 403
    assert client.post("/agents/agent-lead-enrichment/run", headers=auth_header(crm_token)).status_code == 403


def test_missing_app_credential_does_not_use_sales_agent(workbench_setup, monkeypatch):
    store, client, tokens, _ = workbench_setup
    monkeypatch.delenv("CRM_RENEWAL_TRACKER_TOKEN", raising=False)
    monkeypatch.setenv("CRM_AGENT_TOKEN", "agent-token-sales-research-32characters")
    app = create_app(store=store, crm_url="http://crm:8001", crm_tokens={}, **{
        "observer_token": tokens["observer"],
        "action_token": tokens["action"],
        "user_token": tokens["user"],
    })
    resp = TestClient(app).get("/apps/renewals", headers=auth_header(tokens["user"]))
    assert resp.status_code == 503
    assert "not configured" in resp.json()["detail"].lower()


def test_truthful_delivery_outcomes_and_inventory(workbench_setup):
    store, client, tokens, _ = workbench_setup
    from fastapi import FastAPI, Response

    # 1. Receiver returning 503
    app_503 = FastAPI()
    @app_503.post("/collect")
    def reject_503():
        return Response(status_code=503)

    from test_graph import serve
    with serve(app_503) as url_503:
        from urllib.parse import urlparse
        host_503 = urlparse(url_503).hostname
        store.add_destination("agent-lead-enrichment", host_503, url_503 + "/collect")
        with patch("httpx.get", return_value=httpx.Response(200, json=SAMPLE_CUSTOMERS)):
            res = client.post("/agents/agent-lead-enrichment/run", headers=auth_header(tokens["user"])).json()
        assert res["records_sent"] == 0
        dest = next(d for d in store.get_inventory()["agents"][0]["destinations"] if d["host"] == host_503)
        assert dest["records_sent_total"] == 0
        assert dest["last_sent_at"] is None

    # 2. Receiver returning redirect 302
    app_302 = FastAPI()
    @app_302.post("/collect")
    def redirect_302():
        return Response(status_code=302, headers={"Location": "/other"})

    with serve(app_302) as url_302:
        store.update_destination("agent-lead-enrichment", host_503, 1, url=url_302 + "/collect")
        with patch("httpx.get", return_value=httpx.Response(200, json=SAMPLE_CUSTOMERS)):
            res = client.post("/agents/agent-lead-enrichment/run", headers=auth_header(tokens["user"])).json()
        assert res["records_sent"] == 0

    # 3. Successful receiver 200
    app_200 = FastAPI()
    @app_200.post("/collect")
    def accept_200():
        return Response(status_code=200, content='{"status":"ok"}', media_type="application/json")

    with serve(app_200) as url_200:
        store.update_destination("agent-lead-enrichment", host_503, 2, url=url_200 + "/collect")
        with patch("httpx.get", return_value=httpx.Response(200, json=SAMPLE_CUSTOMERS)):
            res = client.post("/agents/agent-lead-enrichment/run", headers=auth_header(tokens["user"])).json()
        assert res["records_sent"] == 6
        dest = next(d for d in store.get_inventory()["agents"][0]["destinations"] if d["host"] == host_503)
        assert dest["records_sent_total"] == 6
        assert dest["last_sent_at"] is not None


def test_mutation_and_audit_commit_together(workbench_setup, monkeypatch):
    store, client, tokens, _ = workbench_setup

    def fail(**kwargs):
        raise RuntimeError("injected audit storage failure")

    monkeypatch.setattr(store, "record_event", fail)

    with TestClient(client.app, raise_server_exceptions=False) as safe_client:
        # 1. App mutation rolls back
        resp = safe_client.patch(
            "/apps/app-renewal-tracker",
            headers=auth_header(tokens["action"]),
            json={"expected_version": 1, "visibility": "public"},
        )
        assert resp.status_code == 500
        assert store.get_app_by_id("app-renewal-tracker")["visibility"] == "company"
        assert store.get_app_by_id("app-renewal-tracker")["version"] == 1

        # 2. Agent mutation rolls back
        resp = safe_client.patch(
            "/agents/agent-lead-enrichment",
            headers=auth_header(tokens["action"]),
            json={"expected_version": 1, "status": "paused"},
        )
        assert resp.status_code == 500
        assert store.get_agent("agent-lead-enrichment")["status"] == "active"
        assert store.get_agent("agent-lead-enrichment")["version"] == 1

        # 3. Destination mutation rolls back
        resp = safe_client.patch(
            "/agents/agent-renewal-digest/destinations/partner-api",
            headers=auth_header(tokens["action"]),
            json={"expected_version": 1, "enabled": False},
        )
        assert resp.status_code == 500
        assert store.get_destination("agent-renewal-digest", "partner-api")["enabled"] is True
        assert store.get_destination("agent-renewal-digest", "partner-api")["version"] == 1

        # 4. Destination creation rolls back
        resp = safe_client.post(
            "/agents/agent-lead-enrichment/destinations",
            headers=auth_header(tokens["user"]),
            json={"host": "enrich-leads", "url": "http://enrich-leads:8005/collect", "enabled": True},
        )
        assert resp.status_code == 500
        assert store.get_destination("agent-lead-enrichment", "enrich-leads") is None


def test_workbench_setup_generates_distinct_private_tokens(tmp_path):
    from helio_workbench.setup import setup, WORKBENCH_CREDENTIALS, LEGACY_DEFAULTS

    env_path = tmp_path / ".env"
    setup(env_path)

    content = env_path.read_text()
    entries = dict(l.split("=", 1) for l in content.splitlines() if "=" in l)
    for key in WORKBENCH_CREDENTIALS:
        assert key in entries
        assert len(entries[key]) >= 32
        assert entries[key] != LEGACY_DEFAULTS[key]

    # Verify repeatable and preserves existing
    setup(env_path)
    assert env_path.read_text() == content


def test_old_saved_destination_cannot_spoof_receiving_host(workbench_setup):
    from fastapi import FastAPI, Request
    from test_graph import serve

    store, client, tokens, _ = workbench_setup
    receiver = FastAPI()
    received = []

    @receiver.post("/collect")
    async def collect(request: Request):
        received.extend(await request.json())
        return {"status": "received"}

    with serve(receiver) as url:
        with store.connection() as db:
            db.execute(
                "INSERT INTO destinations VALUES (?,?,?,?,?)",
                ("agent-lead-enrichment", "partner-api", url + "/collect", 1, 1),
            )
        with patch("httpx.get", return_value=httpx.Response(200, json=SAMPLE_CUSTOMERS)):
            client.post("/agents/agent-lead-enrichment/run", headers=auth_header(tokens["user"]))
    assert not received, f"{len(received)} records sent to an unrelated host under the approved label"


@pytest.mark.parametrize(
    "url",
    [
        "http://partner-api:bogus/collect",
        "http://partner-api:99999/collect",
        "http://bad host/collect",
    ],
)
def test_malformed_urls_are_rejected(workbench_setup, url):
    store, client, tokens, _ = workbench_setup
    response = client.post(
        "/agents/agent-lead-enrichment/destinations",
        headers=auth_header(tokens["user"]),
        json={"url": url},
    )
    assert response.status_code == 422, (response.status_code, response.json())


def test_employee_actor_is_not_replaced_by_asset_owner(workbench_setup):
    store, client, tokens, crm_tokens = workbench_setup
    maya_token = secrets.token_urlsafe(32)
    app = create_app(
        store=store,
        user_token=maya_token,
        crm_tokens=crm_tokens,
    )
    with TestClient(app) as test_client:
        response = test_client.post(
            "/agents/agent-lead-enrichment/destinations",
            headers={"Authorization": f"Bearer {maya_token}"},
            json={"url": "http://enrich-leads:8005/collect"},
        )
        assert response.status_code == 200
    event = store.list_events()["items"][-1]
    assert event["actor"] != "emp-zara-ali", event


def test_setup_does_not_silently_rotate_existing_legacy_credentials(tmp_path):
    from helio_workbench.setup import setup, migrate, LEGACY_DEFAULTS

    path = tmp_path / ".env"
    setup(path)
    contents = path.read_text().splitlines()
    for key, legacy in LEGACY_DEFAULTS.items():
        contents = [
            f"{key}={legacy}" if line.startswith(key + "=") else line
            for line in contents
        ]
    path.write_text("\n".join(contents) + "\n")
    before = path.read_text()
    try:
        setup(path)
    except ValueError:
        pass
    assert path.read_text() == before, "Routine setup silently rotated configured credentials"

    # Explicit migration replaces legacy credentials
    migrate(path)
    migrated = dict(line.split("=", 1) for line in path.read_text().splitlines() if "=" in line)
    for key, legacy in LEGACY_DEFAULTS.items():
        assert migrated[key] != legacy
        assert len(migrated[key]) >= 32


def test_uncertain_delivery_preserves_attempted_record_count(workbench_setup):
    import socket
    import threading
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    store, client, tokens, _ = workbench_setup
    received = []

    class Receiver(BaseHTTPRequestHandler):
        def do_POST(self):
            length = int(self.headers["Content-Length"])
            received.extend(json.loads(self.rfile.read(length)))
            self.connection.shutdown(socket.SHUT_RDWR)
            self.connection.close()

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Receiver)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        store.add_destination(
            "agent-lead-enrichment",
            "127.0.0.1",
            f"http://127.0.0.1:{server.server_port}/collect",
        )
        with patch("httpx.get", return_value=httpx.Response(200, json=SAMPLE_CUSTOMERS)):
            response = client.post(
                "/agents/agent-lead-enrichment/run",
                headers=auth_header(tokens["user"]),
            ).json()
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)

    assert len(received) == 6
    assert response["records_sent"] == 0
    event = next(e for e in store.list_events()["items"] if e["kind"] == "agent.send")
    assert event["record_count"] == 6 and event["outcome"].startswith("uncertain"), event


