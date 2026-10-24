import json
import secrets

import pytest
from fastapi.testclient import TestClient

from helio_crm.api import create_app
from helio_crm.credentials import generate_credentials
from helio_crm.store import CRMStore


def bearer(token):
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture
def observer_crm(directory, tmp_path):
    tokens = {key: secrets.token_urlsafe(32) for key in ("admin", "maya", "agent", "observer")}
    store = CRMStore(tmp_path / "crm.sqlite3")
    with TestClient(create_app(store.path, tokens, directory.employees)) as client:
        yield client, store, tokens


def test_observer_only_reads_metadata(observer_crm):
    client, _, tokens = observer_crm
    headers = bearer(tokens["observer"])
    for path in ("/accounts", "/grants", "/inventory"):
        assert client.get(path, headers=headers).status_code == 200
    assert client.get("/customers", headers=headers).status_code == 403
    assert client.get("/audit", headers=headers).status_code == 403
    assert client.patch("/customers/cust-northstar", headers=headers,
                        json={"expected_version": 1, "notes": "no"}).status_code == 403
    assert client.post("/grants/grant-sales-agent/revoke", headers=headers,
                       json={"expected_version": 1}).status_code == 403


def test_inventory_is_atomic_metadata_without_customer_records_or_credentials(observer_crm):
    client, _, tokens = observer_crm
    inventory = client.get("/inventory", headers=bearer(tokens["observer"])).json()
    assert inventory["company_id"] == "helio"
    assert inventory["complete"] is True
    assert inventory["schema_version"] == 1
    assert len(inventory["accounts"]) == 23
    assert inventory["app"]["id"] == "helio-crm"
    assert {r["id"] for r in inventory["resources"]} == {"customer-database", "crm-metadata"}
    serialized = json.dumps(inventory)
    assert "Northstar" not in serialized and "token_hash" not in serialized
    assert all(t not in serialized for t in tokens.values())
    observer_grant = next(g for g in inventory["grants"] if g["id"] == "grant-security-observer")
    assert observer_grant["resource_id"] == "crm-metadata"
    assert client.get("/inventory", headers=bearer(tokens["agent"])).status_code == 403


def test_revoking_observer_removes_metadata_access_and_restart_does_not_restore_it(observer_crm, directory):
    client, store, tokens = observer_crm
    assert client.post("/grants/grant-security-observer/revoke", headers=bearer(tokens["admin"]),
                       json={"expected_version": 1}).status_code == 200
    store.initialize([], tokens)
    assert client.get("/inventory", headers=bearer(tokens["observer"])).status_code == 403


def test_legacy_crm_upgrade_preserves_data_tokens_and_revocation(directory, tmp_path):
    tokens = {key: secrets.token_urlsafe(32) for key in ("admin", "maya", "agent")}
    store = CRMStore(tmp_path / "crm.sqlite3")
    store.initialize(directory.employees(), tokens)
    store.revoke_grant(tokens["admin"], "grant-sales-agent", 1)
    store.update_customer(tokens["maya"], "cust-northstar", 1, {"notes": "Keep this note"})
    # Construct the actual previous schema when the new implementation is present.
    with store.connection() as db:
        columns = {r["name"] for r in db.execute("PRAGMA table_info(grants)")}
        if "resource_id" in columns:
            db.execute("ALTER TABLE grants DROP COLUMN resource_id")
        db.execute("UPDATE metadata SET value='1' WHERE key='schema_version'")
        original_hashes = dict(db.execute("SELECT principal_id, token_hash FROM credentials"))
    tokens["observer"] = secrets.token_urlsafe(32)
    store.initialize([], tokens)
    store.initialize([], tokens)
    assert store.get_customer(tokens["maya"], "cust-northstar")["notes"] == "Keep this note"
    assert next(g for g in store.list_grants(tokens["observer"]) if g["id"] == "grant-sales-agent")["status"] == "revoked"
    with store.connection() as db:
        current = dict(db.execute("SELECT principal_id, token_hash FROM credentials"))
        assert all(current[k] == v for k, v in original_hashes.items())
        assert db.execute("SELECT COUNT(*) FROM credentials").fetchone()[0] == 4


def test_setup_adds_observer_without_rotating_existing_secrets(tmp_path):
    path = tmp_path / ".env"
    old = {f"CRM_{name}_TOKEN": secrets.token_urlsafe(32) for name in ("ADMIN", "MAYA", "AGENT")}
    path.write_text("".join(f"{k}={v}\n" for k, v in old.items()) + "NEO4J_PASSWORD=preserve-this-value\n")
    generate_credentials(path)
    new = dict(line.split("=", 1) for line in path.read_text().splitlines())
    assert all(new[k] == v for k, v in old.items())
    assert new["NEO4J_PASSWORD"] == "preserve-this-value"
    assert len(new["CRM_OBSERVER_TOKEN"]) >= 32
    assert path.stat().st_mode & 0o777 == 0o600


def test_directory_snapshot_contains_current_status_and_consistent_memberships(client, directory):
    directory.set_employee_status("emp-maya-chen", "inactive")
    response = client.get("/snapshot")
    assert response.status_code == 200
    snapshot = response.json()
    assert snapshot["complete"] is True and snapshot["company_id"] == "helio"
    assert len(snapshot["teams"]) == 5 and len(snapshot["employees"]) == 20
    assert all(e["team_id"] in {t["id"] for t in snapshot["teams"]} for e in snapshot["employees"])
    assert next(e for e in snapshot["employees"] if e["id"] == "emp-maya-chen")["status"] == "inactive"


def test_setup_rejects_ambiguous_duplicate_credentials_without_editing_file(tmp_path):
    path = tmp_path / ".env"
    generate_credentials(path)
    with path.open("a") as output:
        output.write("CRM_OBSERVER_TOKEN=" + secrets.token_urlsafe(32) + "\n")
    before = path.read_text()
    with pytest.raises(ValueError, match="Refusing"):
        generate_credentials(path)
    assert path.read_text() == before


def test_observer_token_mismatch_does_not_rotate_identity(observer_crm):
    _, store, tokens = observer_crm
    with pytest.raises(ValueError, match="credentials"):
        store.initialize([], {**tokens, "observer": secrets.token_urlsafe(32)})
    assert store.inventory(tokens["observer"])["complete"] is True
