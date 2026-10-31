"""Upgrade actual pre-activity DDL; never synthesize old schemas via new initializers."""

import secrets
import sqlite3
import uuid

import pytest

from helio_crm.store import CRMError, CRMStore, token_hash
from test_crm_activity import events, pending


@pytest.fixture(params=[1, 2])
def legacy_crm(tmp_path, request):
    version = request.param
    path = tmp_path / "legacy.sqlite3"
    tokens = {key: secrets.token_urlsafe(32) for key in ("admin", "maya", "agent")}
    with sqlite3.connect(path) as db:
        db.executescript("""
            CREATE TABLE metadata (key TEXT PRIMARY KEY NOT NULL, value TEXT NOT NULL);
            CREATE TABLE employees (id TEXT PRIMARY KEY NOT NULL, name TEXT NOT NULL,
                email TEXT NOT NULL UNIQUE, team_id TEXT NOT NULL);
            CREATE TABLE principals (id TEXT PRIMARY KEY NOT NULL, name TEXT NOT NULL,
                kind TEXT NOT NULL CHECK(kind IN ('employee','agent','service')),
                owner_employee_id TEXT NOT NULL REFERENCES employees(id),
                role TEXT NOT NULL CHECK(role IN ('user','admin')));
            CREATE TABLE credentials (token_hash TEXT PRIMARY KEY NOT NULL,
                principal_id TEXT NOT NULL UNIQUE REFERENCES principals(id));
            CREATE TABLE grants (id TEXT PRIMARY KEY NOT NULL,
                principal_id TEXT NOT NULL REFERENCES principals(id), scopes TEXT NOT NULL,
                status TEXT NOT NULL CHECK(status IN ('active','revoked')),
                version INTEGER NOT NULL DEFAULT 1);
            CREATE TABLE customers (id TEXT PRIMARY KEY NOT NULL, name TEXT NOT NULL,
                contact_email TEXT NOT NULL, plan TEXT NOT NULL,
                subscription_status TEXT NOT NULL CHECK(subscription_status IN ('active','trial','cancelled')),
                notes TEXT NOT NULL, version INTEGER NOT NULL DEFAULT 1);
            CREATE TABLE audit (id INTEGER PRIMARY KEY AUTOINCREMENT, occurred_at TEXT NOT NULL,
                request_id TEXT NOT NULL UNIQUE, actor_id TEXT, action TEXT NOT NULL,
                resource TEXT NOT NULL, outcome TEXT NOT NULL, status_code INTEGER NOT NULL,
                detail TEXT NOT NULL);
            INSERT INTO employees VALUES ('emp-maya-chen','Maya','maya@helio.example','sales'),
                ('emp-casey-wong','Casey','casey@helio.example','leadership');
            INSERT INTO principals VALUES ('employee:emp-maya-chen','Maya','employee','emp-maya-chen','user'),
                ('agent:sales-research','Agent','agent','emp-maya-chen','user'),
                ('service:crm-admin','Admin','service','emp-casey-wong','admin');
            INSERT INTO grants VALUES ('grant-maya','employee:emp-maya-chen',
                '["customers:read","customers:write"]','active',7),
                ('grant-sales-agent','agent:sales-research','["customers:read"]','revoked',9);
            INSERT INTO customers VALUES ('cust-legacy','Legacy','private@example.test','growth',
                'active','preserved private note',4);
            INSERT INTO audit VALUES (42,'2026-01-01T00:00:00+00:00','old-request',
                'service:crm-admin','grants:revoke','grant-sales-agent','allowed',200,'Operation completed');
        """)
        if version == 2:
            db.execute("ALTER TABLE grants ADD COLUMN resource_id TEXT NOT NULL DEFAULT 'customer-database'")
        db.execute("INSERT INTO metadata VALUES ('schema_version', ?)", (str(version),))
        principals = {"admin": "service:crm-admin", "maya": "employee:emp-maya-chen", "agent": "agent:sales-research"}
        db.executemany("INSERT INTO credentials VALUES (?,?)", [(token_hash(v), principals[k]) for k, v in tokens.items()])
    return CRMStore(path, activity_enabled=True), tokens, version


def snapshot(store):
    with store.connection() as db:
        return {name: [dict(row) for row in db.execute(f"SELECT * FROM {name}")]
                for name in ("employees", "principals", "credentials", "customers", "audit")}


def test_real_legacy_migration_preserves_data_and_never_backfills_events(legacy_crm):
    store, tokens, _ = legacy_crm
    before = snapshot(store)
    store.initialize([], tokens)
    assert snapshot(store) == before
    assert events(store) == [] and pending(store) == []
    with store.connection() as db:
        instance = db.execute("SELECT value FROM metadata WHERE key='activity_source_instance_id'").fetchone()[0]
        assert uuid.UUID(instance)
        assert db.execute("SELECT value FROM metadata WHERE key='schema_version'").fetchone()[0] == "3"
        row = db.execute("SELECT status,version,resource_id FROM grants WHERE id='grant-sales-agent'").fetchone()
        assert tuple(row) == ("revoked", 9, "customer-database")
    store.initialize([], tokens)
    assert snapshot(store) == before
    assert store.get_customer(tokens["maya"], "cust-legacy")["notes"] == "preserved private note"
    with pytest.raises(CRMError) as caught:
        store.list_customers(tokens["agent"])
    assert caught.value.status_code == 403
    assert {event["source_instance_id"] for event in events(store)} == {instance}


def test_legacy_credential_mismatch_does_not_partially_migrate(legacy_crm):
    store, tokens, version = legacy_crm
    before = snapshot(store)
    with pytest.raises(ValueError, match="credentials"):
        store.initialize([], {**tokens, "agent": secrets.token_urlsafe(32)})
    assert snapshot(store) == before
    with store.connection() as db:
        assert db.execute("SELECT value FROM metadata WHERE key='schema_version'").fetchone()[0] == str(version)
        assert not db.execute("SELECT name FROM sqlite_master WHERE name='activity_events'").fetchone()


def test_migration_exception_rolls_back_ddl_observer_and_version(legacy_crm, monkeypatch):
    from helio_crm import activity
    store, tokens, version = legacy_crm
    before = snapshot(store)
    original = activity.initialize

    def broken(db):
        original(db)
        raise RuntimeError("injected migration failure")

    with monkeypatch.context() as patch:
        patch.setattr(activity, "initialize", broken)
        with pytest.raises(RuntimeError, match="migration failure"):
            store.initialize([], {**tokens, "observer": secrets.token_urlsafe(32)})
    assert snapshot(store) == before
    with store.connection() as db:
        assert db.execute("SELECT value FROM metadata WHERE key='schema_version'").fetchone()[0] == str(version)
        assert not db.execute("SELECT name FROM sqlite_master WHERE name='activity_events'").fetchone()
        assert ("resource_id" in {row["name"] for row in db.execute("PRAGMA table_info(grants)")}) == (version == 2)
    store.initialize([], tokens)
    assert events(store) == []


@pytest.mark.parametrize("damage", ["missing_epoch", "invalid_epoch", "missing_outbox"])
def test_current_schema_corruption_is_not_silently_reinitialized(legacy_crm, damage):
    store, tokens, _ = legacy_crm
    store.initialize([], tokens)
    with store.connection() as db:
        if damage == "missing_epoch":
            db.execute("DELETE FROM metadata WHERE key='activity_source_instance_id'")
        elif damage == "invalid_epoch":
            db.execute("UPDATE metadata SET value='not-an-instance' WHERE key='activity_source_instance_id'")
        else:
            db.execute("DROP TABLE activity_outbox")
    with pytest.raises(ValueError, match="activity schema"):
        store.initialize([], tokens)


def test_restart_and_disabled_capture_preserve_pending_evidence_and_epoch(legacy_crm):
    store, tokens, _ = legacy_crm
    store.initialize([], tokens)
    store.get_customer(tokens["maya"], "cust-legacy")
    before = events(store)
    disabled = CRMStore(store.path)
    disabled.initialize([], tokens)
    disabled.get_customer(tokens["maya"], "cust-legacy")
    assert events(disabled) == before and len(pending(disabled)) == 1
    restarted = CRMStore(store.path, activity_enabled=True)
    restarted.initialize([], tokens)
    restarted.get_customer(tokens["maya"], "cust-legacy")
    after = events(restarted)
    assert after[0] == before[0]
    assert after[1]["sequence"] > after[0]["sequence"]
    assert after[1]["source_instance_id"] == after[0]["source_instance_id"]
    assert len(pending(restarted)) == 2


def test_failed_fresh_initialization_leaves_no_half_seeded_database(directory, tmp_path, monkeypatch):
    from helio_crm import activity
    store = CRMStore(tmp_path / "fresh.sqlite3", activity_enabled=True)
    tokens = {key: secrets.token_urlsafe(32) for key in ("admin", "maya", "agent")}
    original = activity.initialize

    def fail(db):
        original(db)
        raise RuntimeError("injected initialization failure")

    with monkeypatch.context() as patch:
        patch.setattr(activity, "initialize", fail)
        with pytest.raises(RuntimeError):
            store.initialize(directory.employees(), tokens)
    assert store.is_initialized() is False
    with store.connection() as db:
        assert db.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall() == []
    store.initialize(directory.employees(), tokens)
    assert len(store.list_customers(tokens["agent"])) == 6
    assert len(events(store)) == len(pending(store)) == 1


def test_new_database_has_new_instance_and_unsupported_schema_is_unchanged(directory, tmp_path):
    tokens = {key: secrets.token_urlsafe(32) for key in ("admin", "maya", "agent")}
    instances = []
    for name in ("one", "two"):
        store = CRMStore(tmp_path / (name + ".sqlite3"), activity_enabled=True)
        store.initialize(directory.employees(), tokens)
        store.list_customers(tokens["agent"])
        instances.append(events(store)[0]["source_instance_id"])
    assert len(set(instances)) == 2
    with store.connection() as db:
        db.execute("UPDATE metadata SET value='999' WHERE key='schema_version'")
    before = events(store)
    with pytest.raises(ValueError, match="Unsupported CRM schema"):
        store.initialize([], tokens)
    assert events(store) == before


@pytest.mark.parametrize("legacy_crm", [2], indirect=True)
def test_revoked_observer_and_its_existing_credential_survive_activity_upgrade(legacy_crm):
    store, tokens, _ = legacy_crm
    tokens["observer"] = secrets.token_urlsafe(32)
    with store.connection() as db:
        db.execute("INSERT INTO principals VALUES ('service:security-observer','Observer','service','emp-casey-wong','user')")
        db.execute("INSERT INTO credentials VALUES (?,'service:security-observer')", (token_hash(tokens["observer"]),))
        db.execute("""INSERT INTO grants VALUES ('grant-security-observer','service:security-observer',
            '["accounts:read","grants:read","inventory:read"]','revoked',5,'crm-metadata')""")
    before = snapshot(store)
    store.initialize([], tokens)
    assert snapshot(store) == before
    with pytest.raises(CRMError) as caught:
        store.inventory(tokens["observer"])
    assert caught.value.status_code == 403
    assert events(store)[0]["actor_id"] == "service:security-observer"
