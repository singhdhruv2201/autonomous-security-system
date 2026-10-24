"""Real Neo4j integration tests; each test owns and cleans up its own namespace."""

import os
import secrets
import time
import uuid
from contextlib import contextmanager

import pytest
from neo4j import GraphDatabase

from helio_crm.store import CRMStore
from helio_security.graph import GraphStore
from helio_security.models import normalize
from helio_security.sync import sync_all

pytestmark = pytest.mark.neo4j


@pytest.fixture
def graph():
    uri = os.getenv("NEO4J_TEST_URI")
    if not uri:
        pytest.skip("Set NEO4J_TEST_URI and NEO4J_PASSWORD to run real database tests")
    driver = GraphDatabase.driver(uri, auth=("neo4j", os.environ["NEO4J_PASSWORD"]))
    company = "test-" + uuid.uuid4().hex
    store = GraphStore(driver, company_id=company)
    store.initialize()
    try:
        yield store
    finally:
        driver.execute_query("MATCH (n {company_id:$company}) DETACH DELETE n", company=company,
                             database_="neo4j")
        driver.close()


@pytest.fixture
def sources(directory, tmp_path):
    tokens = {key: secrets.token_urlsafe(32) for key in ("admin", "maya", "agent", "observer")}
    crm = CRMStore(tmp_path / "crm.sqlite3")
    crm.initialize(directory.employees(), tokens)
    return directory, crm, tokens


def sync(graph, sources, at):
    directory, crm, tokens = sources
    graph.apply(normalize("directory", directory.snapshot()), at)
    graph.apply(normalize("crm", crm.inventory(tokens["observer"])), at)


def query(graph, cypher, **kwargs):
    return graph.driver.execute_query(cypher, company=graph.company_id,
                                      database_="neo4j", **kwargs).records


def test_repeatable_graph_has_provenance_and_no_credentials_or_customer_content(graph, sources):
    now = time.time()
    sync(graph, sources, now)
    before = graph.status(now)["counts"]
    sync(graph, sources, now + 1)
    assert graph.status(now + 1)["counts"] == before
    assert before["Employee"] == 20 and before["Team"] == 5
    assert before["App"] == 1 and before["Agent"] == 1 and before["DataResource"] == 2
    assert before["AccessGrant"] == 3 and before["AppAccount"] == 20
    rows = query(graph, "MATCH (a {company_id:$company})-[r]->() RETURN properties(r) AS p")
    assert rows and all(r["p"]["source"] in {"directory", "crm"} for r in rows)
    assert all(r["p"]["first_seen"] == now and r["p"]["last_observed_at"] == now + 1 for r in rows)
    nodes = str(query(graph, "MATCH (n {company_id:$company}) RETURN properties(n) AS p"))
    assert "Northstar" not in nodes and "token_hash" not in nodes
    assert all(token not in nodes for token in sources[2].values())


def test_offboarding_and_revocation_change_observed_risk_without_enforcement(graph, sources):
    directory, crm, tokens = sources
    now = time.time()
    sync(graph, sources, now)
    assert graph.risks(now)["findings"] == []
    directory.set_employee_status("emp-maya-chen", "inactive")
    sync(graph, sources, now + 1)
    result = graph.risks(now + 1)
    assert result["coverage_complete"] is True
    assert len(result["findings"]) == 1
    assert result["findings"][0]["grant_id"] == "grant-sales-agent"
    assert len(crm.list_customers(tokens["agent"])) == 6
    crm.revoke_grant(tokens["admin"], "grant-sales-agent", 1)
    sync(graph, sources, now + 2)
    assert graph.risks(now + 2)["findings"] == []
    rows = query(graph, "MATCH (g:AccessGrant {company_id:$company, id:'grant-sales-agent'}) RETURN g.status AS status, g.version AS version")
    assert dict(rows[0]) == {"status": "revoked", "version": 2}


def test_failure_retains_evidence_marks_coverage_stale_and_recovery_clears_error(graph, sources):
    now = time.time()
    sources[0].set_employee_status("emp-maya-chen", "inactive")
    sync(graph, sources, now)
    graph.fail("crm", "http_403", now + 1)
    result = graph.risks(now + 1)
    assert result["coverage_complete"] is False and len(result["findings"]) == 1
    assert result["findings"][0]["evidence_fresh"] is False
    assert graph.status(now + 1)["sources"]["crm"]["error"] == "http_403"
    sync(graph, sources, now + 2)
    assert graph.status(now + 2)["coverage_complete"] is True
    assert graph.status(now + 2)["sources"]["crm"]["error"] is None
    assert graph.risks(now + 1000)["coverage_complete"] is False


def test_missing_grant_is_unobserved_not_falsely_revoked(graph, sources):
    now = time.time()
    sync(graph, sources, now)
    payload = sources[1].inventory(sources[2]["observer"])
    payload["grants"] = [g for g in payload["grants"] if g["id"] != "grant-sales-agent"]
    graph.apply(normalize("crm", payload), now + 1)
    rows = query(graph, "MATCH (g:AccessGrant {company_id:$company,id:'grant-sales-agent'}) RETURN g.present AS present, g.status AS status")
    assert dict(rows[0]) == {"present": False, "status": "active"}
    rows = query(graph, "MATCH (a {company_id:$company})-[r:HAS_GRANT]->(g {id:'grant-sales-agent'}) RETURN r.present AS present")
    assert rows[0]["present"] is False


def test_out_of_order_sync_and_failures_cannot_replace_newer_observations(graph, sources):
    now = time.time()
    sync(graph, sources, now)
    old = normalize("directory", sources[0].snapshot())
    sources[0].set_employee_status("emp-maya-chen", "inactive")
    sync(graph, sources, now + 2)
    assert graph.apply(old, now + 1) is False
    assert graph.fail("directory", "unavailable", now + 1) is False
    assert graph.risks(now + 2)["coverage_complete"] is True
    assert len(graph.risks(now + 2)["findings"]) == 1


def test_missing_directory_rolls_back_entire_crm_import(graph, sources):
    payload = normalize("crm", sources[1].inventory(sources[2]["observer"]))
    with pytest.raises(ValueError, match="endpoint"):
        graph.apply(payload, time.time())
    assert graph.status()["counts"] == {}
    assert graph.status()["coverage_complete"] is False


def test_graph_namespaces_are_isolated(graph, sources):
    now = time.time()
    sync(graph, sources, now)
    other = GraphStore(graph.driver, company_id="test-empty-" + uuid.uuid4().hex)
    assert other.status()["counts"] == {}
    assert other.risks()["findings"] == []


@contextmanager
def serve(app):
    import socket
    import threading
    import uvicorn

    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        url = f"http://127.0.0.1:{sock.getsockname()[1]}"
        server = uvicorn.Server(uvicorn.Config(app, log_level="error", ws="none"))
        thread = threading.Thread(target=server.run, kwargs={"sockets": [sock]}, daemon=True)
        thread.start()
        try:
            deadline = time.monotonic() + 5
            while not server.started and thread.is_alive() and time.monotonic() < deadline:
                time.sleep(0.01)
            assert server.started
            yield url
        finally:
            server.should_exit = True
            thread.join(5)
            assert not thread.is_alive()


def test_real_api_connectors_to_real_neo4j_and_revoked_observer(graph, sources):
    from helio_simulator.api import create_app as directory_app
    from helio_crm.api import create_app as crm_app
    directory, crm, tokens = sources
    with serve(directory_app(directory.path)) as directory_url, serve(
            crm_app(crm.path, tokens, directory.employees)) as crm_url:
        assert sync_all(graph, directory_url, crm_url, tokens["observer"]) == {"directory": "ok", "crm": "ok"}
        directory.set_employee_status("emp-maya-chen", "inactive")
        sync_all(graph, directory_url, crm_url, tokens["observer"])
        assert len(graph.risks()["findings"]) == 1
        crm.revoke_grant(tokens["admin"], "grant-security-observer", 1)
        result = sync_all(graph, directory_url, crm_url, tokens["observer"])
        assert result == {"directory": "ok", "crm": "http_403"}
        assert graph.risks()["coverage_complete"] is False
        assert len(graph.risks()["findings"]) == 1


def test_failed_update_rolls_back_retirement_of_existing_graph(graph, sources):
    now = time.time()
    sync(graph, sources, now)
    before = graph.status(now)
    payload = sources[1].inventory(sources[2]["observer"])
    payload["app"]["owner_employee_id"] = "missing-employee"
    with pytest.raises(ValueError, match="endpoint"):
        graph.apply(normalize("crm", payload), now + 1)
    assert graph.status(now) == before


def test_parallel_repeated_snapshots_do_not_duplicate_graph(graph, sources):
    from concurrent.futures import ThreadPoolExecutor
    now = time.time()
    sync(graph, sources, now)
    before = graph.status(now)["counts"]
    snapshot = normalize("crm", sources[1].inventory(sources[2]["observer"]))
    with ThreadPoolExecutor(max_workers=3) as pool:
        list(pool.map(lambda offset: graph.apply(snapshot, now + offset), [1, 2, 3]))
    assert graph.status(now + 3)["counts"] == before
    assert graph.status(now + 3)["sources"]["crm"]["last_success"] == now + 3
