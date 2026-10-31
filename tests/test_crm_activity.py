"""Durable CRM evidence: real SQLite transactions, not mocked persistence."""

import json
import secrets
import sqlite3
import uuid

import pytest

from helio_crm.store import CRMError, CRMStore


@pytest.fixture
def activity_crm(directory, tmp_path):
    tokens = {key: secrets.token_urlsafe(32) for key in ("admin", "maya", "agent", "observer")}
    store = CRMStore(tmp_path / "crm.sqlite3", activity_enabled=True)
    store.initialize(directory.employees(), tokens)
    return store, tokens


def events(store, table="activity_events"):
    assert table in {"activity_events", "operational_events"}
    with store.connection() as db:
        return [{**json.loads(row["payload"]),
                 **{k: row[k] for k in ("sequence", "event_id", "request_id", "occurred_at")}}
                for row in db.execute(f"SELECT * FROM {table} ORDER BY sequence")]


def pending(store):
    with store.connection() as db:
        return [dict(row) for row in db.execute("SELECT event_sequence,status FROM activity_outbox ORDER BY event_sequence")]


def test_default_off_still_migrates_but_keeps_legacy_audit(directory, tmp_path):
    tokens = {key: secrets.token_urlsafe(32) for key in ("admin", "maya", "agent")}
    store = CRMStore(tmp_path / "crm.sqlite3")
    store.initialize(directory.employees(), tokens)
    assert len(store.list_customers(tokens["agent"])) == 6
    assert events(store) == []
    assert pending(store) == []
    assert events(store, "operational_events") == []
    with store.connection() as db:
        assert db.execute("SELECT COUNT(*) FROM audit").fetchone()[0] == 1
        assert db.execute("SELECT value FROM metadata WHERE key='schema_version'").fetchone()[0] == "4"


def test_read_produces_one_redacted_event_and_pending_delivery(activity_crm):
    store, tokens = activity_crm
    assert len(store.list_customers(tokens["agent"])) == 6
    [event] = events(store)
    assert event["sequence"] == 1
    assert event["schema_version"] == 1
    assert event["event_type"] == "operation.completed"
    assert event["company_id"] == "helio" and event["source"] == "helio-crm"
    assert event["stream"] == "security"
    assert event["actor_id"] == "agent:sales-research"
    assert event["actor_kind"] == "agent"
    assert event["owner_employee_id"] == "emp-maya-chen"
    assert event["action"] == "customers:read"
    assert event["resource_id"] == "customer-database" and event["record_id"] is None
    assert event["outcome"] == "allowed" and event["status_code"] == 200
    assert event["reason_code"] == "operation_completed"
    assert event["result_record_count"] == 6
    assert event["changed_fields"] == []
    assert event["before_version"] is None and event["after_version"] is None
    for name in ("event_id", "request_id", "source_instance_id"):
        assert str(uuid.UUID(event[name])) == event[name]
    assert pending(store) == [{"event_sequence": event["sequence"], "status": "pending"}]
    with store.connection() as db:
        audit = dict(db.execute("SELECT * FROM audit").fetchone())
    assert event["request_id"] == audit["request_id"]
    assert event["occurred_at"] == audit["occurred_at"]
    serialized = json.dumps(event)
    assert "Northstar" not in serialized and "token_hash" not in serialized
    assert all(token not in serialized for token in tokens.values())


def test_mutation_records_committed_fields_without_values(activity_crm):
    store, tokens = activity_crm
    private = "secret customer note: " + tokens["maya"]
    result = store.update_customer(tokens["maya"], "cust-northstar", 1, {"notes": private})
    assert result["notes"] == private and result["version"] == 2
    [event] = events(store)
    assert event["record_id"] == "cust-northstar"
    assert event["changed_fields"] == ["notes"]
    assert (event["before_version"], event["after_version"]) == (1, 2)
    assert event["result_record_count"] is None
    assert private not in json.dumps(event) and tokens["maya"] not in json.dumps(event)
    assert len(pending(store)) == 1


def test_metadata_and_audit_success_are_operational_but_denials_export(activity_crm):
    store, tokens = activity_crm
    store.inventory(tokens["observer"])
    store.list_accounts(tokens["observer"])
    store.list_grants(tokens["admin"])
    result = store.audit(tokens["admin"])
    assert len(result) == 3  # Audit reads preserve the existing collect-then-log behavior.
    assert events(store) == [] and pending(store) == []
    operational = events(store, "operational_events")
    assert [e["sequence"] for e in operational] == [1, 2, 3, 4]
    assert all(e["stream"] == "operational" for e in operational)
    with pytest.raises(CRMError) as caught:
        store.audit(tokens["agent"])
    assert caught.value.status_code == 403
    [event] = events(store)
    assert event["sequence"] == 1 and event["reason_code"] == "access_denied"
    assert len(pending(store)) == 1


@pytest.mark.parametrize("token_kind, status, reason", [
    ("unknown", 401, "authentication_failed"),
    ("agent", 403, "access_denied"),
])
def test_denials_have_trusted_actor_and_no_side_effects(activity_crm, token_kind, status, reason):
    store, tokens = activity_crm
    token = tokens.get(token_kind, "untrusted-token-value")
    with pytest.raises(CRMError) as caught:
        store.update_customer(token, "cust-northstar", 1, {"notes": "must not commit"})
    assert caught.value.status_code == status
    [event] = events(store)
    assert event["reason_code"] == reason and event["outcome"] == "denied"
    assert event["actor_id"] == ("agent:sales-research" if token_kind == "agent" else None)
    assert event["changed_fields"] == [] and event["after_version"] is None
    assert token not in json.dumps(event)
    with store.connection() as db:
        row = db.execute("SELECT notes, version FROM customers WHERE id='cust-northstar'").fetchone()
        assert tuple(row) == ("", 1)
    assert len(pending(store)) == 1


@pytest.mark.parametrize("failure", [CRMError(409, "private conflict payload"), RuntimeError("private exception payload")])
def test_partial_operation_rolls_back_but_failure_evidence_commits(activity_crm, failure):
    store, tokens = activity_crm

    def broken_operation(db):
        db.execute("UPDATE customers SET notes='partial',version=2 WHERE id='cust-northstar'")
        raise failure

    with pytest.raises(CRMError) as caught:
        store._perform(tokens["maya"], "customers:write", "cust-northstar", broken_operation)
    assert caught.value.status_code == (409 if isinstance(failure, CRMError) else 500)
    with store.connection() as db:
        assert tuple(db.execute("SELECT notes,version FROM customers WHERE id='cust-northstar'").fetchone()) == ("", 1)
        audit = dict(db.execute("SELECT * FROM audit").fetchone())
    [event] = events(store)
    assert event["outcome"] == "failed"
    assert event["changed_fields"] == [] and event["after_version"] is None
    assert event["reason_code"] == ("version_conflict" if isinstance(failure, CRMError) else "operation_failed")
    assert "private" not in json.dumps([event, audit])
    assert len(pending(store)) == 1


@pytest.mark.parametrize("table", ["audit", "activity_events", "activity_outbox"])
def test_evidence_write_failure_rolls_back_business_and_all_audits(activity_crm, table):
    store, tokens = activity_crm
    with store.connection() as db:
        db.execute(f"""CREATE TRIGGER reject_evidence BEFORE INSERT ON {table}
            BEGIN SELECT RAISE(ABORT, 'private database failure'); END""")
    with pytest.raises(CRMError) as caught:
        store.update_customer(tokens["maya"], "cust-northstar", 1, {"notes": "must roll back"})
    assert caught.value.status_code == 503
    assert "private" not in caught.value.detail
    with store.connection() as db:
        assert tuple(db.execute("SELECT notes,version FROM customers WHERE id='cust-northstar'").fetchone()) == ("", 1)
        assert db.execute("SELECT COUNT(*) FROM audit").fetchone()[0] == 0
    assert events(store) == [] and pending(store) == []


def test_unknown_ids_and_invalid_field_names_never_enter_evidence(activity_crm):
    store, tokens = activity_crm
    private = "secret-token-" + tokens["maya"]
    with pytest.raises(CRMError) as missing:
        store.get_customer(tokens["maya"], private)
    assert missing.value.status_code == 404
    with pytest.raises(CRMError) as invalid:
        store.update_customer(tokens["maya"], "cust-northstar", 1, {private: private})
    assert invalid.value.status_code == 422
    with store.connection() as db:
        audit = [dict(row) for row in db.execute("SELECT * FROM audit")]
    assert private not in json.dumps([events(store), audit])
    assert events(store)[0]["record_id"] is None


def test_operational_log_failure_cannot_return_metadata(activity_crm):
    store, tokens = activity_crm
    with store.connection() as db:
        db.execute("""CREATE TRIGGER reject_operational BEFORE INSERT ON operational_events
            BEGIN SELECT RAISE(ABORT, 'private failure'); END""")
    with pytest.raises(CRMError) as caught:
        store.inventory(tokens["observer"])
    assert caught.value.status_code == 503
    assert events(store, "operational_events") == []
    with store.connection() as db:
        assert db.execute("SELECT COUNT(*) FROM audit").fetchone()[0] == 0


@pytest.mark.parametrize("enabled", ["0", "1", 0, 1, None, []])
def test_store_rejects_non_boolean_capture_configuration(tmp_path, enabled):
    with pytest.raises(ValueError, match="activity_enabled"):
        CRMStore(tmp_path / "unused.sqlite3", activity_enabled=enabled)


@pytest.mark.parametrize("limit", [0, -1, True, "10", 1.5, 1000001])
def test_store_rejects_unbounded_or_invalid_activity_limits(tmp_path, limit):
    with pytest.raises(ValueError, match="activity_limit"):
        CRMStore(tmp_path / "unused.sqlite3", activity_limit=limit)


def test_security_capacity_refuses_write_without_losing_existing_evidence(activity_crm):
    original, tokens = activity_crm
    store = CRMStore(original.path, activity_enabled=True, activity_limit=1)
    store.list_customers(tokens["agent"])
    before = events(store)
    with pytest.raises(CRMError) as caught:
        store.update_customer(tokens["maya"], "cust-northstar", 1, {"notes": "over capacity"})
    assert caught.value.status_code == 503
    assert events(store) == before and len(pending(store)) == 1
    with store.connection() as db:
        assert tuple(db.execute("SELECT notes,version FROM customers WHERE id='cust-northstar'").fetchone()) == ("", 1)
        assert db.execute("SELECT COUNT(*) FROM audit").fetchone()[0] == 1
    assert store.inventory(tokens["observer"])["complete"] is True  # Independent stream.


def test_operational_capacity_refuses_metadata_without_export_recursion(activity_crm):
    original, tokens = activity_crm
    store = CRMStore(original.path, activity_enabled=True, activity_limit=1)
    store.inventory(tokens["observer"])
    with pytest.raises(CRMError) as caught:
        store.audit(tokens["admin"])
    assert caught.value.status_code == 503
    assert len(events(store, "operational_events")) == 1
    assert events(store) == [] and pending(store) == []
    assert len(store.list_customers(tokens["agent"])) == 6


def test_commit_failure_rolls_back_customer_event_and_outbox(activity_crm):
    store, tokens = activity_crm
    with store.connection() as db:
        db.execute("CREATE TABLE guard_parent(id INTEGER PRIMARY KEY)")
        db.execute("""CREATE TABLE guard_child(id INTEGER REFERENCES guard_parent(id)
            DEFERRABLE INITIALLY DEFERRED)""")
        db.execute("""CREATE TRIGGER fail_at_commit AFTER INSERT ON activity_outbox
            BEGIN INSERT INTO guard_child VALUES (99); END""")
    with pytest.raises(CRMError) as caught:
        store.update_customer(tokens["maya"], "cust-northstar", 1, {"notes": "never committed"})
    assert caught.value.status_code == 503
    assert events(store) == [] and pending(store) == []
    with store.connection() as db:
        assert tuple(db.execute("SELECT notes,version FROM customers WHERE id='cust-northstar'").fetchone()) == ("", 1)
        assert db.execute("SELECT COUNT(*) FROM audit").fetchone()[0] == 0


def test_process_abort_rolls_back_without_inventing_completion(activity_crm):
    store, tokens = activity_crm

    def abort(db):
        db.execute("UPDATE customers SET notes='partial' WHERE id='cust-northstar'")
        raise KeyboardInterrupt()

    with pytest.raises(KeyboardInterrupt):
        store._perform(tokens["maya"], "customers:write", "cust-northstar", abort)
    assert events(store) == [] and pending(store) == []
    with store.connection() as db:
        assert db.execute("SELECT notes FROM customers WHERE id='cust-northstar'").fetchone()[0] == ""


def test_parallel_reads_have_unique_ordered_events_and_exactly_one_pending_row(activity_crm):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier
    original, tokens = activity_crm
    ready = Barrier(6)

    def read(_):
        store = CRMStore(original.path, activity_enabled=True)
        ready.wait(timeout=5)
        return len(store.list_customers(tokens["agent"]))

    with ThreadPoolExecutor(max_workers=6) as pool:
        assert list(pool.map(read, range(24))) == [6] * 24
    recorded = events(original)
    assert [e["sequence"] for e in recorded] == list(range(1, 25))
    assert len({e["event_id"] for e in recorded}) == len({e["request_id"] for e in recorded}) == 24
    assert [r["event_sequence"] for r in pending(original)] == [e["sequence"] for e in recorded]
    assert len({e["source_instance_id"] for e in recorded}) == 1


def test_parallel_capacity_check_cannot_overfill_stream(activity_crm):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier
    original, tokens = activity_crm
    ready = Barrier(2)

    def read(_):
        store = CRMStore(original.path, activity_enabled=True, activity_limit=1)
        ready.wait(timeout=5)
        try:
            store.list_customers(tokens["agent"])
            return 200
        except CRMError as error:
            return error.status_code

    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sorted(pool.map(read, range(2))) == [200, 503]
    assert len(events(original)) == len(pending(original)) == 1


def test_parallel_revocations_log_both_attempts_but_one_change(activity_crm):
    from concurrent.futures import ThreadPoolExecutor
    store, tokens = activity_crm
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: store.revoke_grant(tokens["admin"], "grant-sales-agent", 1), range(2)))
    assert results[0] == results[1] and results[0]["version"] == 2
    first, second = events(store)
    assert first["changed_fields"] == ["status"]
    assert (first["before_version"], first["after_version"]) == (1, 2)
    assert second["changed_fields"] == []
    assert (second["before_version"], second["after_version"]) == (2, 2)
    assert first["record_id"] == second["record_id"] == "grant-sales-agent"
    with pytest.raises(CRMError) as caught:
        store.list_customers(tokens["agent"])
    assert caught.value.status_code == 403


def test_counts_and_field_names_describe_actual_result_not_requested_changes(activity_crm):
    store, tokens = activity_crm
    store.get_customer(tokens["agent"], "cust-northstar")
    store.update_customer(tokens["maya"], "cust-northstar", 1, {"notes": "", "subscription_status": "trial"})
    with pytest.raises(CRMError):
        store.update_customer(tokens["maya"], "cust-northstar", 1, {"notes": "stale"})
    with store.connection() as db:
        db.execute("DELETE FROM customers")  # Isolated fixture, never a product deletion operation.
    assert store.list_customers(tokens["agent"]) == []
    recorded = events(store)
    assert recorded[0]["result_record_count"] == 1
    assert recorded[1]["changed_fields"] == ["subscription_status"]
    assert recorded[2]["reason_code"] == "version_conflict" and recorded[2]["changed_fields"] == []
    assert recorded[3]["result_record_count"] == 0


def test_empty_or_secret_missing_grant_id_is_not_logged(activity_crm):
    store, tokens = activity_crm
    for record_id in ("", tokens["admin"]):
        with pytest.raises(CRMError) as caught:
            store.revoke_grant(tokens["admin"], record_id, 1)
        assert caught.value.status_code == 404
    assert all(e["record_id"] is None for e in events(store))
    with store.connection() as db:
        audit = [dict(row) for row in db.execute("SELECT * FROM audit")]
    assert tokens["admin"] not in json.dumps([events(store), audit])


def test_database_full_inside_operation_returns_safe_error_without_partial_changes(activity_crm, monkeypatch):
    from contextlib import contextmanager
    store, tokens = activity_crm
    with store.connection() as db:
        db.execute("CREATE TABLE storage_probe(content BLOB)")
    original = store.connection

    @contextmanager
    def limited():
        with original() as db:
            pages = db.execute("PRAGMA page_count").fetchone()[0]
            db.execute(f"PRAGMA max_page_count={pages}")
            yield db

    def fill(db):
        db.execute("UPDATE customers SET notes='partial' WHERE id='cust-northstar'")
        db.execute("INSERT INTO storage_probe VALUES (zeroblob(1048576))")

    with monkeypatch.context() as patch:
        patch.setattr(store, "connection", limited)
        with pytest.raises(CRMError) as caught:
            store._perform(tokens["maya"], "customers:write", "cust-northstar", fill)
    assert caught.value.status_code == 503
    assert events(store) == [] and pending(store) == []
    with store.connection() as db:
        assert db.execute("SELECT notes FROM customers WHERE id='cust-northstar'").fetchone()[0] == ""
        assert db.execute("SELECT COUNT(*) FROM audit").fetchone()[0] == 0


def test_write_lock_timeout_is_safe_and_does_not_log_a_completed_operation(activity_crm, monkeypatch):
    from contextlib import contextmanager
    store, tokens = activity_crm
    original = store.connection

    @contextmanager
    def impatient():
        with original() as db:
            db.execute("PRAGMA busy_timeout=1")
            yield db

    with original() as lock, monkeypatch.context() as patch:
        lock.execute("BEGIN IMMEDIATE")
        patch.setattr(store, "connection", impatient)
        with pytest.raises(CRMError) as caught:
            store.list_customers(tokens["agent"])
        assert caught.value.status_code == 503
    assert events(store) == [] and pending(store) == []


def test_outbox_cannot_reference_missing_event(activity_crm):
    store, _ = activity_crm
    with pytest.raises(sqlite3.IntegrityError), store.connection() as db:
        db.execute("INSERT INTO activity_outbox(event_sequence) VALUES (999)")
    assert pending(store) == []
