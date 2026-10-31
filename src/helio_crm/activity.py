"""Allowlisted operation evidence and a transaction-local outbox; no transport."""

import json
import sqlite3
import uuid


RESOURCES = {
    "customers:read": "customer-database",
    "customers:write": "customer-database",
    "grants:revoke": "crm-metadata",
    "accounts:read": "crm-metadata",
    "grants:read": "crm-metadata",
    "inventory:read": "crm-metadata",
    "audit:read": "crm-audit",
    "activity:replay": "crm-metadata",
    "events:read": "crm-metadata",
}
OPERATIONAL_READS = {"accounts:read", "grants:read", "inventory:read", "audit:read", "activity:replay", "events:read"}
REASONS = {200: "operation_completed", 401: "authentication_failed", 403: "access_denied",
           404: "resource_not_found", 409: "version_conflict", 422: "invalid_operation"}


class CapacityError(Exception):
    """The enabled local evidence stream cannot retain another operation."""


def initialize(db):
    """Called inside the CRM schema migration transaction, including on v1 upgrades."""
    for table in ("activity_events", "operational_events"):
        db.execute(f"""CREATE TABLE IF NOT EXISTS {table} (
            sequence INTEGER PRIMARY KEY AUTOINCREMENT,
            event_id TEXT NOT NULL UNIQUE,
            request_id TEXT NOT NULL,
            occurred_at TEXT NOT NULL,
            payload TEXT NOT NULL CHECK(json_valid(payload)))""")
    db.execute("""CREATE TABLE IF NOT EXISTS activity_outbox (
        event_sequence INTEGER PRIMARY KEY REFERENCES activity_events(sequence),
        status TEXT NOT NULL DEFAULT 'pending' CHECK(status = 'pending'))""")
    db.execute("INSERT OR IGNORE INTO metadata VALUES ('activity_source_instance_id', ?)",
               (str(uuid.uuid4()),))


def validate_schema(db, *, delivery=False):
    """A current database must not silently acquire a new source identity."""
    try:
        row = db.execute("SELECT value FROM metadata WHERE key='activity_source_instance_id'").fetchone()
        if row is None:
            raise ValueError()
        uuid.UUID(row[0])
        for table in ("activity_events", "operational_events"):
            db.execute(f"SELECT sequence,event_id,request_id,occurred_at,payload FROM {table} LIMIT 0")
        db.execute("SELECT event_sequence,status FROM activity_outbox LIMIT 0")
        if delivery:
            db.execute('SELECT attempts,next_attempt_at,lease_id,lease_until,last_status,last_error FROM activity_outbox LIMIT 0')
            db.execute('SELECT id,event_sequence,occurred_at,status_code,outcome,actor_id FROM delivery_audit LIMIT 0')
    except (sqlite3.Error, ValueError, TypeError):
        raise ValueError("Invalid CRM activity schema; restore the database before starting") from None


def migrate_outbox(db):
    db.execute('ALTER TABLE activity_outbox RENAME TO activity_outbox_v3')
    db.execute('''CREATE TABLE activity_outbox (
        event_sequence INTEGER PRIMARY KEY REFERENCES activity_events(sequence),
        status TEXT NOT NULL DEFAULT 'pending' CHECK(status IN ('pending','inflight','delivered','dead_letter')),
        attempts INTEGER NOT NULL DEFAULT 0, next_attempt_at REAL NOT NULL DEFAULT 0,
        lease_id TEXT, lease_until REAL, last_status INTEGER, last_error TEXT)''')
    db.execute('INSERT INTO activity_outbox(event_sequence,status) SELECT event_sequence,status FROM activity_outbox_v3')
    db.execute('DROP TABLE activity_outbox_v3')
    db.execute('CREATE INDEX delivery_due ON activity_outbox(status,next_attempt_at,lease_until)')
    db.execute('''CREATE TABLE delivery_audit(id INTEGER PRIMARY KEY AUTOINCREMENT,
        event_sequence INTEGER NOT NULL, occurred_at REAL NOT NULL,status_code INTEGER NOT NULL,
        outcome TEXT NOT NULL, actor_id TEXT NOT NULL)''')


def record_snapshot(db, action, resource):
    if action in {"customers:read", "customers:write"}:
        row = db.execute("SELECT id, notes, subscription_status, version FROM customers WHERE id=?",
                         (resource,)).fetchone()
    elif action == "grants:revoke":
        row = db.execute("SELECT id, status, version FROM grants WHERE id=?", (resource,)).fetchone()
    else:
        row = None
    return dict(row) if row else None


def append(db, *, request_id, occurred_at, principal, action, resource,
           status, outcome, result, before, limit):
    """Persist metadata only. Never serialize the operation arguments, results or error."""
    operational = action in OPERATIONAL_READS and status == 200
    changed, before_version, after_version = [], None, None
    if status == 200 and before is not None and action in {"customers:write", "grants:revoke"}:
        after = record_snapshot(db, action, resource)
        fields = ("notes", "subscription_status") if action == "customers:write" else ("status",)
        changed = [field for field in fields if before[field] != after[field]]
        before_version, after_version = before["version"], after["version"]
    payload = {
        "schema_version": 1, "event_type": "operation.completed",
        "company_id": "helio", "source": "helio-crm",
        "source_instance_id": db.execute(
            "SELECT value FROM metadata WHERE key='activity_source_instance_id'").fetchone()[0],
        "stream": "operational" if operational else "security",
        "actor_id": principal["id"] if principal else None,
        "actor_kind": principal["kind"] if principal else None,
        "owner_employee_id": principal["owner_employee_id"] if principal else None,
        "action": action, "resource_id": RESOURCES[action],
        "record_id": before["id"] if before else None,
        "outcome": outcome, "status_code": status,
        "reason_code": REASONS.get(status, "operation_failed"),
        "result_record_count": (len(result) if isinstance(result, list) else 1)
        if status == 200 and action == "customers:read" else None,
        "changed_fields": changed, "before_version": before_version, "after_version": after_version,
    }
    from .lifecycle import context
    ctx = context.get()
    if ctx is not None:
        ctx.update({key: payload[key] for key in ('actor_id', 'actor_kind', 'owner_employee_id', 'result_record_count')})
    persist(db, payload, request_id, occurred_at, limit)


def persist(db, payload, request_id, occurred_at, limit):
    # The surrounding BEGIN IMMEDIATE makes the capacity check and append atomic.
    # Delivered evidence remains retained and still counts toward this limit.
    operational = payload['stream'] == 'operational'
    table = 'operational_events' if operational else 'activity_events'
    if db.execute(f'SELECT COUNT(*) FROM {table}').fetchone()[0] >= limit:
        raise CapacityError()
    cursor = db.execute(f"""INSERT INTO {table}(event_id,request_id,occurred_at,payload)
        VALUES (?,?,?,?)""", (str(uuid.uuid4()), request_id, occurred_at,
                              json.dumps(payload, sort_keys=True, separators=(",", ":"))))
    if not operational:
        db.execute("INSERT INTO activity_outbox(event_sequence) VALUES (?)", (cursor.lastrowid,))
