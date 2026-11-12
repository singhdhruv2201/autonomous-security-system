"""Full HTTP -> worker -> customer APIs -> Neo4j -> persisted findings path."""

import json
import secrets
import time
import urllib.request

import pytest

from test_graph import graph, workbench_sources, serve
from helio_workbench.api import create_app as workbench_app
from helio_crm.api import create_app as crm_app
from helio_simulator.api import create_app as directory_app
from helio_security.api import create_app
from helio_security.settings import Settings

pytestmark = pytest.mark.neo4j


def request(url, settings, path, method="GET"):
    req = urllib.request.Request(url + path, method=method,
        headers={"Authorization": "Bearer " + settings.operator_token, "Content-Type": "application/json"},
        data=b"{}" if method == "POST" else None)
    with urllib.request.urlopen(req, timeout=5) as response:
        return json.load(response)


def synchronize(url, settings):
    run = request(url, settings, "/sync-runs", "POST")
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        run = request(url, settings, "/sync-runs/" + run["id"])
        if run["status"] not in {"queued", "running"}:
            return run
        time.sleep(0.03)
    pytest.fail("Background observation did not finish")


def test_real_platform_observation_lifecycle_and_restart(graph, workbench_sources, tmp_path):
    directory, crm, tokens, wb = workbench_sources
    with serve(directory_app(directory.path)) as directory_url, serve(
            crm_app(crm.path, tokens, directory.employees)) as crm_url, serve(
            workbench_app(store=wb,observer_token=tokens["observer"],action_token="a"*32,user_token="u"*32)) as workbench_url:
        settings = Settings(operator_token=secrets.token_urlsafe(32), viewer_token=secrets.token_urlsafe(32),
            observer_token=tokens["observer"], company_id=graph.company_id,
            db_path=tmp_path / "security.sqlite3", directory_url=directory_url, crm_url=crm_url, sync_interval=0,
            workbench_url=workbench_url, workbench_observer_token=tokens["observer"])
        with serve(create_app(settings, graph=graph)) as platform:
            assert synchronize(platform, settings)["status"] == "succeeded"
            assert request(platform, settings, "/overview")["counts"]["Employee"] == 20
            inventory = request(platform, settings, "/graph")
            assert len(inventory["nodes"]) == 69 and len(inventory["edges"]) == 101
            assert request(platform, settings, "/findings")["total"] == 0
            directory.set_employee_status("emp-maya-chen", "inactive")
            assert synchronize(platform, settings)["status"] == "succeeded"
            item = request(platform, settings, "/findings")["items"][0]
            assert item["status"] == "open" and item["evidence_fresh"] is True
            finding_id = item["id"]
            assert len(crm.list_customers(tokens["agent"])) == 6  # Observation does not enforce.
            synchronize(platform, settings)
            assert request(platform, settings, "/findings/" + finding_id)["event_count"] == 1
            crm.revoke_grant(tokens["admin"], "grant-security-observer", 1)
            assert synchronize(platform, settings)["status"] == "partial"
            stale = request(platform, settings, "/findings/" + finding_id)
            assert stale["status"] == "open" and stale["assessment"] == "unknown"
            # Restore only this isolated test's observer to exercise recovery.
            with crm.connection() as db:
                db.execute("UPDATE grants SET status='active',version=version+1 WHERE id='grant-security-observer'")
            assert synchronize(platform, settings)["status"] == "succeeded"
            crm.revoke_grant(tokens["admin"], "grant-sales-agent", 1)
            assert synchronize(platform, settings)["status"] == "succeeded"
            resolved = request(platform, settings, "/findings/" + finding_id)
            assert resolved["status"] == "resolved" and resolved["reason"] == "grant_revoked"
            assert resolved["events"][-1]["type"] == "resolved"
            serialized = json.dumps({"graph": inventory, "finding": resolved,
                                     "audit": request(platform, settings, "/audit")})
            assert all(token not in serialized for token in [*tokens.values(), settings.operator_token, settings.viewer_token])
            assert "Northstar" not in serialized and "token_hash" not in serialized
        with serve(create_app(settings, graph=graph)) as restarted:
            assert request(restarted, settings, "/findings/" + finding_id)["status"] == "resolved"
            assert request(restarted, settings, "/sync-runs")["total"] == 6
