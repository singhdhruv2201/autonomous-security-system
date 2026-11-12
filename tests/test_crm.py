"""Contract tests for real CRM permissions, persistence, and activity records."""

import hashlib
import json

import pytest
from fastapi.testclient import TestClient

from helio_crm.api import create_app
from helio_crm.store import CRMError, CRMStore
from helio_simulator.directory import Directory


@pytest.fixture
def crm_setup(tmp_path):
    directory = Directory(tmp_path / "directory.sqlite3")
    directory.initialize()
    # Per-test synthetic credentials, not deployment credentials.
    import secrets
    tokens = {key: secrets.token_urlsafe(32) for key in ("admin", "maya", "agent")}
    store = CRMStore(tmp_path / "crm.sqlite3")
    store.initialize(directory.employees(), tokens)
    return directory, store, tokens


def auth(token):
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture
def crm_client(crm_setup):
    directory, store, tokens = crm_setup
    with TestClient(create_app(store.path, tokens, directory.employees)) as client:
        yield client, tokens


def test_agent_reads_but_cannot_edit_or_administer(crm_client):
    client, tokens = crm_client
    response = client.get("/customers", headers=auth(tokens["agent"]))
    assert response.status_code == 200
    assert len(response.json()) == 6
    customer = response.json()[0]
    assert client.get(f"/customers/{customer['id']}", headers=auth(tokens["agent"])).json() == customer
    assert client.patch(f"/customers/{customer['id']}", headers=auth(tokens["agent"]),
                        json={"expected_version": 1, "notes": "not allowed"}).status_code == 403
    for path in ("/accounts", "/grants", "/audit"):
        assert client.get(path, headers=auth(tokens["agent"])).status_code == 403
    assert client.post("/grants/grant-sales-agent/revoke", headers=auth(tokens["agent"]),
                       json={"expected_version": 1}).status_code == 403


def test_employee_edit_is_persistent_and_rejects_stale_or_invalid_changes(crm_client, crm_setup):
    client, tokens = crm_client
    path = "/customers/cust-northstar"
    response = client.patch(path, headers=auth(tokens["maya"]),
                            json={"expected_version": 1, "notes": "Renewal scheduled"})
    assert response.status_code == 200
    assert response.json()["version"] == 2
    assert response.json()["notes"] == "Renewal scheduled"
    assert client.patch(path, headers=auth(tokens["maya"]),
                        json={"expected_version": 1, "notes": "Stale overwrite"}).status_code == 409
    assert client.patch(path, headers=auth(tokens["maya"]),
                        json={"expected_version": 2, "subscription_status": "bogus"}).status_code == 422
    assert client.patch(path, headers=auth(tokens["maya"]),
                        json={"expected_version": 2, "owner_employee_id": "other"}).status_code == 422
    directory, store, _ = crm_setup
    with TestClient(create_app(store.path, tokens, directory.employees)) as restarted:
        assert restarted.get(path, headers=auth(tokens["maya"])).json()["notes"] == "Renewal scheduled"


def test_offboarding_does_not_revoke_independent_agent_credential(crm_client, crm_setup):
    client, tokens = crm_client
    directory, _, _ = crm_setup
    directory.set_employee_status("emp-maya-chen", "inactive")
    assert next(e for e in directory.employees() if e["id"] == "emp-maya-chen")["status"] == "inactive"
    assert client.get("/customers", headers=auth(tokens["agent"])).status_code == 200
    grants = client.get("/grants", headers=auth(tokens["admin"])).json()
    agent_grant = next(g for g in grants if g["id"] == "grant-sales-agent")
    assert agent_grant["owner_employee_id"] == "emp-maya-chen"
    assert agent_grant["status"] == "active"


def test_revocation_is_enforced_idempotent_and_survives_restart(crm_client, crm_setup):
    client, tokens = crm_client
    path = "/grants/grant-sales-agent/revoke"
    assert client.post(path, headers=auth(tokens["admin"]),
                       json={"expected_version": 999}).status_code == 409
    assert client.get("/customers", headers=auth(tokens["agent"])).status_code == 200
    first = client.post(path, headers=auth(tokens["admin"]), json={"expected_version": 1})
    assert first.status_code == 200
    assert first.json()["status"] == "revoked"
    assert first.json()["version"] == 2
    assert client.post(path, headers=auth(tokens["admin"]),
                       json={"expected_version": 1}).json() == first.json()
    assert client.get("/customers", headers=auth(tokens["agent"])).status_code == 403
    assert client.get("/customers", headers=auth(tokens["maya"])).status_code == 200
    directory, store, _ = crm_setup
    with TestClient(create_app(store.path, tokens, directory.employees)) as restarted:
        assert restarted.get("/customers", headers=auth(tokens["agent"])).status_code == 403


def test_audit_records_allow_deny_and_actor_without_credentials(crm_client):
    client, tokens = crm_client
    assert client.get("/customers").status_code == 401
    assert client.get("/customers", headers=auth("invalid-token")).status_code == 401
    assert client.get("/customers", headers=auth(tokens["agent"])).status_code == 200
    assert client.get("/grants", headers=auth(tokens["agent"])).status_code == 403
    records = client.get("/audit", headers=auth(tokens["admin"])).json()
    assert any(r["actor_id"] == "agent:sales-research" and r["outcome"] == "allowed" for r in records)
    assert any(r["actor_id"] == "agent:sales-research" and r["outcome"] == "denied" for r in records)
    assert sum(r["status_code"] == 401 for r in records) == 2
    assert all(r["occurred_at"] and r["request_id"] for r in records)
    serialized = json.dumps(records)
    assert "invalid-token" not in serialized
    assert all(token not in serialized for token in tokens.values())


def test_store_seeds_real_accounts_and_only_stores_token_hashes(crm_setup):
    _, store, tokens = crm_setup
    with store.connection() as db:
        assert db.execute("SELECT COUNT(*) FROM principals WHERE kind='employee'").fetchone()[0] == 20
        rows = db.execute("SELECT token_hash FROM credentials").fetchall()
        assert {r[0] for r in rows} == {hashlib.sha256(t.encode()).hexdigest() for t in tokens.values()}
        assert db.execute("SELECT owner_employee_id FROM principals WHERE id='agent:sales-research'").fetchone()[0] == "emp-maya-chen"


def test_invalid_bootstrap_does_not_partially_seed(tmp_path):
    import secrets
    store = CRMStore(tmp_path / "crm.sqlite3")
    tokens = {key: secrets.token_urlsafe(32) for key in ("admin", "maya", "agent")}
    with pytest.raises(ValueError, match="Maya"):
        store.initialize([], tokens)
    assert not store.path.exists()


def test_unknown_customer_and_grant_return_404(crm_client):
    client, tokens = crm_client
    assert client.get("/customers/missing", headers=auth(tokens["agent"])).status_code == 404
    assert client.post("/grants/missing/revoke", headers=auth(tokens["admin"]),
                       json={"expected_version": 1}).status_code == 404


def test_restart_rejects_mismatched_credentials_without_changing_data(crm_setup):
    directory, store, tokens = crm_setup
    with pytest.raises(ValueError, match="credentials"):
        store.initialize(directory.employees(), {**tokens, "agent": "x" * 43})
    assert len(store.list_customers(tokens["agent"])) == 6


def test_revocation_at_store_boundary_cannot_be_bypassed(crm_setup):
    _, store, tokens = crm_setup
    store.revoke_grant(tokens["admin"], "grant-sales-agent", 1)
    with pytest.raises(CRMError) as error:
        store.list_customers(tokens["agent"])
    assert error.value.status_code == 403


def test_parallel_revocations_apply_only_one_state_change(crm_setup):
    from concurrent.futures import ThreadPoolExecutor
    _, store, tokens = crm_setup
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: store.revoke_grant(tokens["admin"], "grant-sales-agent", 1), range(2)))
    assert results[0] == results[1]
    assert results[0]["version"] == 2


def test_persisted_crm_restarts_without_directory_availability(crm_setup):
    _, store, tokens = crm_setup
    def unavailable():
        raise AssertionError("Existing CRM must not need another identity bootstrap")
    with TestClient(create_app(store.path, tokens, unavailable)) as client:
        assert client.get("/customers", headers=auth(tokens["agent"])).status_code == 200


@pytest.mark.parametrize("changes", [{}, {"notes": None}, {"subscription_status": None}, {"notes": "x" * 501}])
def test_invalid_updates_leave_customer_unchanged(crm_client, changes):
    client, tokens = crm_client
    path = "/customers/cust-northstar"
    before = client.get(path, headers=auth(tokens["maya"])).json()
    assert client.patch(path, headers=auth(tokens["maya"]),
                        json={"expected_version": 1, **changes}).status_code == 422
    assert client.get(path, headers=auth(tokens["maya"])).json() == before


def test_real_http_agent_is_blocked_after_real_http_revocation(crm_setup):
    import socket
    import threading
    import time
    import urllib.error
    import urllib.request
    import uvicorn
    from helio_crm.agent import run

    directory, store, tokens = crm_setup
    # Bind an ephemeral port up front so parallel tests cannot steal it.
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        base = f"http://127.0.0.1:{sock.getsockname()[1]}"
        server = uvicorn.Server(uvicorn.Config(create_app(store.path, tokens, directory.employees),
                                               log_level="error", ws="none"))
        thread = threading.Thread(target=server.run, kwargs={"sockets": [sock]}, daemon=True)
        thread.start()
        try:
            deadline = time.monotonic() + 5
            while not server.started and thread.is_alive() and time.monotonic() < deadline:
                time.sleep(0.01)
            assert server.started, "CRM HTTP server failed to start"
            assert run(base, tokens["agent"])["customer_count"] == 6
            directory.set_employee_status("emp-maya-chen", "inactive")
            assert run(base, tokens["agent"])["customer_count"] == 6
            req = urllib.request.Request(base + "/grants/grant-sales-agent/revoke", method="POST",
                headers={**auth(tokens["admin"]), "Content-Type": "application/json"},
                data=json.dumps({"expected_version": 1}).encode())
            with urllib.request.urlopen(req, timeout=5) as response:
                assert json.load(response)["status"] == "revoked"
            with pytest.raises(urllib.error.HTTPError) as error:
                run(base, tokens["agent"])
            assert error.value.code == 403
        finally:
            server.should_exit = True
            thread.join(timeout=5)
            assert not thread.is_alive()


def test_workbench_principals_can_read_customers(tmp_path):
    import secrets
    from helio_simulator.directory import Directory

    directory = Directory(tmp_path / "directory.sqlite3")
    directory.initialize()

    tokens = {
        "admin": secrets.token_urlsafe(32),
        "maya": secrets.token_urlsafe(32),
        "agent": secrets.token_urlsafe(32),
        "renewal_tracker": secrets.token_urlsafe(32),
        "support_lookup": secrets.token_urlsafe(32),
        "lead_enrichment": secrets.token_urlsafe(32),
        "renewal_digest": secrets.token_urlsafe(32),
    }
    store = CRMStore(tmp_path / "crm.sqlite3")
    store.initialize(directory.employees(), tokens)

    with TestClient(create_app(store.path, tokens, directory.employees)) as client:
        for role, principal_id, owner_id in [
            ("renewal_tracker", "service:renewal-tracker", "emp-luca-rossi"),
            ("support_lookup", "service:support-lookup", "emp-nina-desai"),
            ("lead_enrichment", "agent:lead-enrichment", "emp-zara-ali"),
            ("renewal_digest", "agent:renewal-digest", "emp-hannah-reed"),
        ]:
            token = tokens[role]
            resp = client.get("/customers", headers=auth(token))
            assert resp.status_code == 200, f"{role} failed to read customers: {resp.text}"
            assert len(resp.json()) == 6

        # Check principals and grants directly in db
        with store.connection() as db:
            rows = dict(db.execute("SELECT id, owner_employee_id FROM principals").fetchall())
            assert rows["service:renewal-tracker"] == "emp-luca-rossi"
            assert rows["service:support-lookup"] == "emp-nina-desai"
            assert rows["agent:lead-enrichment"] == "emp-zara-ali"
            assert rows["agent:renewal-digest"] == "emp-hannah-reed"


def test_reconcile_legacy_default_workbench_credentials_without_reset(tmp_path):
    import secrets
    from helio_simulator.directory import Directory

    directory = Directory(tmp_path / "directory.sqlite3")
    directory.initialize()

    base_tokens = {
        "admin": secrets.token_urlsafe(32),
        "maya": secrets.token_urlsafe(32),
        "agent": secrets.token_urlsafe(32),
    }
    legacy_tokens = {
        **base_tokens,
        "renewal_tracker": "token-crm-renewal-tracker-local-workbench",
        "support_lookup": "token-crm-support-lookup-local-workbench",
        "lead_enrichment": "token-crm-lead-enrichment-local-workbench",
        "renewal_digest": "token-crm-renewal-digest-local-workbench",
    }
    store = CRMStore(tmp_path / "crm.sqlite3")
    store.initialize(directory.employees(), legacy_tokens)

    # Re-initialize with new random tokens
    new_tokens = {
        **base_tokens,
        "renewal_tracker": secrets.token_urlsafe(32),
        "support_lookup": secrets.token_urlsafe(32),
        "lead_enrichment": secrets.token_urlsafe(32),
        "renewal_digest": secrets.token_urlsafe(32),
    }
    store.initialize(directory.employees(), new_tokens)

    # Verify new tokens work and legacy tokens fail
    with TestClient(create_app(store.path, new_tokens, directory.employees)) as client:
        for role in ["renewal_tracker", "support_lookup", "lead_enrichment", "renewal_digest"]:
            resp = client.get("/customers", headers=auth(new_tokens[role]))
            assert resp.status_code == 200
            old_resp = client.get("/customers", headers=auth(legacy_tokens[role]))
            assert old_resp.status_code == 401
