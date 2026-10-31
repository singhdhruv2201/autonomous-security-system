"""CRM access is enforced here, against its own database, for every operation."""

import hashlib
import json
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

from .credentials import validate_credentials
from . import activity


class CRMError(Exception):
    def __init__(self, status_code, detail):
        super().__init__(detail)
        self.status_code = status_code
        self.detail = detail


SAFE_DETAILS = {
    "Missing or invalid bearer token", "Administrator credential required",
    "No active grant permits this operation", "Customer not found", "Grant not found",
    "Only notes and subscription_status can be updated", "Notes must be text of at most 500 characters",
    "Invalid subscription status", "Customer changed; fetch current version before editing",
    "Grant changed; fetch current version before revocation", "Operation failed",
}


def token_hash(token):
    return hashlib.sha256(token.encode()).hexdigest()


class CRMStore:
    def __init__(self, path: Path, *, activity_enabled=False, activity_limit=10000):
        if type(activity_enabled) is not bool:
            raise ValueError("activity_enabled must be a boolean")
        if type(activity_limit) is not int or not 1 <= activity_limit <= 1000000:
            raise ValueError("activity_limit must be an integer between 1 and 1000000")
        self.path = path
        self.activity_enabled = activity_enabled
        self.activity_limit = activity_limit

    @contextmanager
    def connection(self):
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys = ON")
        try:
            with db:
                yield db
        finally:
            db.close()

    def is_initialized(self):
        if not self.path.exists():
            return False
        with self.connection() as db:
            if not db.execute("SELECT 1 FROM sqlite_master WHERE name='metadata'").fetchone():
                return False
            return db.execute("SELECT 1 FROM metadata WHERE key='schema_version'").fetchone() is not None

    def initialize(self, employees, tokens):
        validate_credentials(tokens)
        principals = {"admin": "service:crm-admin", "maya": "employee:emp-maya-chen",
                      "agent": "agent:sales-research", "observer": "service:security-observer"}
        if self.is_initialized():
            with self.connection() as db:
                db.execute("BEGIN IMMEDIATE")
                version = db.execute("SELECT value FROM metadata WHERE key='schema_version'").fetchone()[0]
                if version not in {"1", "2", "3"}:
                    raise ValueError("Unsupported CRM schema version")
                existing = {r["principal_id"]: r["token_hash"] for r in db.execute("SELECT * FROM credentials")}
                expected = {principals[k]: token_hash(v) for k, v in tokens.items()}
                if any(expected.get(k) != v for k, v in existing.items()) or any(
                    k not in existing for k in expected if k != "service:security-observer"
                ):
                    raise ValueError("Configured credentials do not match stored credentials; restore the original .env")
                if version == "1":
                    db.execute("ALTER TABLE grants ADD COLUMN resource_id TEXT NOT NULL DEFAULT 'customer-database'")
                    db.execute("UPDATE metadata SET value='2' WHERE key='schema_version'")
                if "observer" in tokens and "service:security-observer" not in existing:
                    self._add_observer(db, tokens["observer"])
                if version != "3":
                    activity.initialize(db)
                    db.execute("UPDATE metadata SET value='3' WHERE key='schema_version'")
                activity.validate_schema(db)
            return
        ids = {e["id"] for e in employees}
        if "emp-maya-chen" not in ids:
            raise ValueError("Directory must contain Maya before CRM bootstrap")
        if "emp-casey-wong" not in ids or len(ids) != len(employees):
            raise ValueError("Directory must contain the CTO and unique employee IDs")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            statements = [
                "CREATE TABLE metadata (key TEXT PRIMARY KEY NOT NULL, value TEXT NOT NULL)",
                """CREATE TABLE employees (id TEXT PRIMARY KEY NOT NULL, name TEXT NOT NULL,
                   email TEXT NOT NULL UNIQUE, team_id TEXT NOT NULL)""",
                """CREATE TABLE principals (id TEXT PRIMARY KEY NOT NULL, name TEXT NOT NULL,
                   kind TEXT NOT NULL CHECK(kind IN ('employee','agent','service')),
                   owner_employee_id TEXT NOT NULL REFERENCES employees(id),
                   role TEXT NOT NULL CHECK(role IN ('user','admin')))""",
                """CREATE TABLE credentials (token_hash TEXT PRIMARY KEY NOT NULL,
                   principal_id TEXT NOT NULL UNIQUE REFERENCES principals(id))""",
                """CREATE TABLE grants (id TEXT PRIMARY KEY NOT NULL,
                   principal_id TEXT NOT NULL REFERENCES principals(id), scopes TEXT NOT NULL,
                   status TEXT NOT NULL CHECK(status IN ('active','revoked')),
                   version INTEGER NOT NULL DEFAULT 1,
                   resource_id TEXT NOT NULL DEFAULT 'customer-database')""",
                """CREATE TABLE customers (id TEXT PRIMARY KEY NOT NULL, name TEXT NOT NULL,
                   contact_email TEXT NOT NULL, plan TEXT NOT NULL,
                   subscription_status TEXT NOT NULL CHECK(subscription_status IN ('active','trial','cancelled')),
                   notes TEXT NOT NULL, version INTEGER NOT NULL DEFAULT 1)""",
                """CREATE TABLE audit (id INTEGER PRIMARY KEY AUTOINCREMENT, occurred_at TEXT NOT NULL,
                   request_id TEXT NOT NULL UNIQUE, actor_id TEXT, action TEXT NOT NULL,
                   resource TEXT NOT NULL, outcome TEXT NOT NULL, status_code INTEGER NOT NULL,
                   detail TEXT NOT NULL)""",
            ]
            for statement in statements:
                db.execute(statement)
            db.executemany("INSERT INTO employees VALUES (?, ?, ?, ?)",
                           [(e["id"], e["name"], e["email"], e["team_id"]) for e in employees])
            db.executemany("INSERT INTO principals VALUES (?, ?, 'employee', ?, 'user')",
                           [(f"employee:{e['id']}", e["name"], e["id"]) for e in employees])
            db.execute("INSERT INTO principals VALUES ('agent:sales-research', 'Sales Research Agent', 'agent', 'emp-maya-chen', 'user')")
            db.execute("INSERT INTO principals VALUES ('service:crm-admin', 'CRM Administration', 'service', 'emp-casey-wong', 'admin')")
            db.executemany("INSERT INTO credentials VALUES (?, ?)",
                           [(token_hash(token), principals[key]) for key, token in tokens.items() if key != "observer"])
            db.executemany("INSERT INTO grants (id,principal_id,scopes,status,version) VALUES (?, ?, ?, 'active', 1)", [
                ("grant-sales-agent", "agent:sales-research", json.dumps(["customers:read"])),
                ("grant-maya", "employee:emp-maya-chen", json.dumps(["customers:read", "customers:write"])),
            ])
            customers = [
                ("northstar", "Northstar Labs", "growth", "active"),
                ("orchard", "Orchard Studio", "starter", "trial"),
                ("bluebird", "Bluebird Logistics", "enterprise", "active"),
                ("cedar", "Cedar Analytics", "growth", "active"),
                ("harbor", "Harbor Design", "starter", "cancelled"),
                ("summit", "Summit Research", "growth", "trial"),
            ]
            db.executemany("INSERT INTO customers VALUES (?, ?, ?, ?, ?, '', 1)", [
                (f"cust-{slug}", name, f"contact@{slug}.example", plan, status)
                for slug, name, plan, status in customers
            ])
            if "observer" in tokens:
                self._add_observer(db, tokens["observer"])
            activity.initialize(db)
            db.execute("INSERT INTO metadata VALUES ('schema_version', '3')")

    @staticmethod
    def _add_observer(db, token):
        db.execute("INSERT INTO principals VALUES ('service:security-observer', 'Security Observer', 'service', 'emp-casey-wong', 'user')")
        db.execute("INSERT INTO credentials VALUES (?, 'service:security-observer')", (token_hash(token),))
        db.execute("""INSERT INTO grants VALUES ('grant-security-observer', 'service:security-observer',
                   ?, 'active', 1, 'crm-metadata')""",
                   (json.dumps(["accounts:read", "grants:read", "inventory:read"]),))

    @contextmanager
    def _operation_connection(self):
        try:
            with self.connection() as db:
                db.execute("BEGIN IMMEDIATE")
                yield db
        except activity.CapacityError:
            raise CRMError(503, "CRM activity capacity reached") from None
        except sqlite3.Error:
            # Covers opening, writes and COMMIT. Never return success if evidence
            # could not be committed, or expose database/trigger exception text.
            raise CRMError(503, "CRM storage is temporarily unavailable") from None

    def _perform(self, token, action, resource, operation, *, admin=False, metadata=False):
        error, result, actor = None, None, None
        with self._operation_connection() as db:
            # Serialize authorization with mutations: revocation takes effect before
            # the next operation's permission check, with no check/use race.
            principal = db.execute("""SELECT p.* FROM credentials c JOIN principals p
                ON c.principal_id=p.id WHERE c.token_hash=?""", (token_hash(token or ""),)).fetchone()
            before = activity.record_snapshot(db, action, resource)
            db.execute("SAVEPOINT crm_operation")
            try:
                if principal is None:
                    raise CRMError(401, "Missing or invalid bearer token")
                actor = principal["id"]
                if admin:
                    if principal["role"] != "admin":
                        raise CRMError(403, "Administrator credential required")
                elif not (metadata and principal["role"] == "admin"):
                    scopes = {scope for row in db.execute(
                        "SELECT scopes FROM grants WHERE principal_id=? AND status='active'", (actor,)
                    ) for scope in json.loads(row["scopes"])}
                    if action not in scopes:
                        raise CRMError(403, "No active grant permits this operation")
                result = operation(db)
            except CRMError as caught:
                db.execute("ROLLBACK TO crm_operation")
                error = CRMError(caught.status_code, caught.detail if caught.detail in SAFE_DETAILS else "Operation failed")
            except sqlite3.Error:
                raise  # Storage failure rolls back the entire operation and audit.
            except Exception:
                db.execute("ROLLBACK TO crm_operation")
                error = CRMError(500, "Operation failed")
            db.execute("RELEASE crm_operation")
            status = error.status_code if error else 200
            outcome = "allowed" if not error else ("denied" if status in (401, 403) else "failed")
            occurred_at, request_id = datetime.now(timezone.utc).isoformat(), str(uuid.uuid4())
            # Unknown path identifiers may themselves contain secrets or log injection.
            safe_resource = before["id"] if before else action.split(":", 1)[0]
            db.execute("""INSERT INTO audit (occurred_at, request_id, actor_id, action,
                resource, outcome, status_code, detail) VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (occurred_at, request_id, actor, action,
                 safe_resource, outcome, status, error.detail if error else "Operation completed"))
            if self.activity_enabled:
                activity.append(db, request_id=request_id, occurred_at=occurred_at,
                                principal=principal, action=action, resource=resource,
                                status=status, outcome=outcome, result=result, before=before,
                                limit=self.activity_limit)
        # Raise only after committing the denial record.
        if error:
            raise error
        return result

    def list_customers(self, token):
        return self._perform(token, "customers:read", "customers",
                             lambda db: [dict(r) for r in db.execute("SELECT * FROM customers ORDER BY name")])

    @staticmethod
    def _customer(db, customer_id):
        row = db.execute("SELECT * FROM customers WHERE id=?", (customer_id,)).fetchone()
        if row is None:
            raise CRMError(404, "Customer not found")
        return dict(row)

    def get_customer(self, token, customer_id):
        return self._perform(token, "customers:read", customer_id, lambda db: self._customer(db, customer_id))

    def update_customer(self, token, customer_id, expected_version, changes):
        def update(db):
            customer = self._customer(db, customer_id)
            if not changes or not set(changes) <= {"notes", "subscription_status"}:
                raise CRMError(422, "Only notes and subscription_status can be updated")
            if "notes" in changes and (not isinstance(changes["notes"], str) or len(changes["notes"]) > 500):
                raise CRMError(422, "Notes must be text of at most 500 characters")
            if "subscription_status" in changes and changes["subscription_status"] not in {"active", "trial", "cancelled"}:
                raise CRMError(422, "Invalid subscription status")
            if customer["version"] != expected_version:
                raise CRMError(409, "Customer changed; fetch current version before editing")
            customer.update(changes)
            db.execute("UPDATE customers SET notes=?, subscription_status=?, version=version+1 WHERE id=?",
                       (customer["notes"], customer["subscription_status"], customer_id))
            return self._customer(db, customer_id)
        return self._perform(token, "customers:write", customer_id, update)

    @staticmethod
    def _grants(db):
        return [{**dict(r), "scopes": json.loads(r["scopes"]), "app_id": "helio-crm"} for r in db.execute("""
            SELECT g.*, p.owner_employee_id FROM grants g JOIN principals p
            ON g.principal_id=p.id ORDER BY g.id""")]

    def list_grants(self, token):
        return self._perform(token, "grants:read", "grants", self._grants, metadata=True)

    def revoke_grant(self, token, grant_id, expected_version):
        def revoke(db):
            grant = next((g for g in self._grants(db) if g["id"] == grant_id), None)
            if grant is None:
                raise CRMError(404, "Grant not found")
            if grant["status"] == "revoked":
                return grant
            if grant["version"] != expected_version:
                raise CRMError(409, "Grant changed; fetch current version before revocation")
            db.execute("UPDATE grants SET status='revoked', version=version+1 WHERE id=?", (grant_id,))
            return next(g for g in self._grants(db) if g["id"] == grant_id)
        return self._perform(token, "grants:revoke", grant_id, revoke, admin=True)

    def list_accounts(self, token):
        return self._perform(token, "accounts:read", "accounts",
                             lambda db: [dict(r) for r in db.execute("SELECT * FROM principals ORDER BY id")], metadata=True)

    def inventory(self, token):
        def snapshot(db):
            return {"schema_version": 1, "company_id": "helio", "complete": True,
                    "app": {"id": "helio-crm", "name": "Helio CRM", "owner_employee_id": "emp-casey-wong"},
                    "resources": [
                        {"id": "customer-database", "name": "Customer database", "classification": "confidential"},
                        {"id": "crm-metadata", "name": "CRM access metadata", "classification": "internal"}],
                    "accounts": [dict(r) for r in db.execute("SELECT * FROM principals ORDER BY id")],
                    "grants": self._grants(db)}
        return self._perform(token, "inventory:read", "inventory", snapshot, metadata=True)

    def audit(self, token, limit=100):
        return self._perform(token, "audit:read", "audit",
                             lambda db: [dict(r) for r in db.execute(
                                 "SELECT * FROM audit ORDER BY id DESC LIMIT ?", (limit,))], admin=True)
