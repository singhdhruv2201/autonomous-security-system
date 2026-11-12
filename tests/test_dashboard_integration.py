"""Dashboard -> security API -> customer APIs -> real Neo4j, isolated company."""

import json
import secrets
import time
import urllib.request

import pytest

from test_graph import graph, workbench_sources, serve
from helio_workbench.api import create_app as workbench_app
from helio_crm.api import create_app as crm_app
from helio_simulator.api import create_app as directory_app
from helio_security.api import create_app as security_app
from helio_security.settings import Settings
from helio_dashboard.api import create_app as dashboard_app
from helio_dashboard.gateway import Gateway

pytestmark = pytest.mark.neo4j


def test_dashboard_real_graph_refresh_and_finding_lifecycle(graph, workbench_sources, tmp_path):
    directory, crm, tokens, wb = workbench_sources
    with serve(directory_app(directory.path)) as directory_url, serve(
            crm_app(crm.path, tokens, directory.employees)) as crm_url, serve(
            workbench_app(store=wb,observer_token=tokens["observer"],action_token="a"*32,user_token="u"*32)) as workbench_url:
        settings = Settings(operator_token=secrets.token_urlsafe(32), viewer_token=secrets.token_urlsafe(32),
            observer_token=tokens["observer"], company_id=graph.company_id,
            db_path=tmp_path / "security.sqlite3", directory_url=directory_url, crm_url=crm_url, sync_interval=0,
            workbench_url=workbench_url, workbench_observer_token=tokens["observer"])
        with serve(security_app(settings, graph=graph)) as security_url, serve(
                dashboard_app(Gateway(security_url, settings.viewer_token, settings.operator_token))) as url:
            def get(path):
                with urllib.request.urlopen(url + path, timeout=10) as response:
                    return json.load(response)

            def refresh():
                req = urllib.request.Request(url + "/api/sync-runs", data=b"{}", method="POST",
                    headers={"Origin": url, "X-Awasi-Request": "dashboard", "Content-Type": "application/json"})
                with urllib.request.urlopen(req, timeout=5) as response:
                    assert response.status == 202
                    job = json.load(response)
                # This is a functional test, not a cold-start latency assertion.
                deadline = time.monotonic() + 60
                while time.monotonic() < deadline:
                    run = get("/api/sync-runs/" + job["id"])
                    if run["status"] not in {"queued", "running"}:
                        assert run["status"] == "succeeded"
                        return
                    time.sleep(.03)
                pytest.fail("Dashboard observation did not finish")

            refresh()
            state = get("/api/state")
            assert state["errors"] == {}
            assert state["overview"]["counts"]["Employee"] == 20
            assert len(state["graph"]["nodes"]) == 69
            assert len(state["graph"]["edges"]) == 101
            assert [p["id"] for p in state["policies"]["items"]] == ["inactive_owner_agent_access", "public_app_confidential_data", "agent_unapproved_destination"]
            assert state["policies"]["items"][0]["enabled"] is True
            assert state["policies"]["items"][0]["coverage_status"] == "current"
            assert "policy_runtime" not in state["overview"]
            assert state["overview"]["observation"]["remediation_enabled"] is False
            directory.set_employee_status("emp-maya-chen", "inactive")
            refresh()
            state = get("/api/state")
            assert state["overview"]["finding_counts"]["open"] == 1
            item = state["findings"]["items"][0]
            assert item["evidence"]["agent_id"] == "agent:sales-research"
            detail = get("/api/findings/" + item["id"])
            assert detail["event_count"] == 1 and detail["evidence_fresh"] is True
            assert len(crm.list_customers(tokens["agent"])) == 6  # UI did not revoke anything.
            crm.revoke_grant(tokens["admin"], "grant-sales-agent", 1)
            refresh()
            state = get("/api/state")
            assert state["overview"]["finding_counts"] == {"open": 0, "resolved": 1}
            assert get("/api/findings/" + item["id"])["events"][-1]["type"] == "resolved"
            serialized = json.dumps(state)
            assert all(secret not in serialized for secret in [*tokens.values(), settings.viewer_token, settings.operator_token])
            assert "Northstar" not in serialized and "token_hash" not in serialized
