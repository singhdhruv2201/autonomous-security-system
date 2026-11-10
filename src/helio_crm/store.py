"""CRM access is enforced here, against its own database, for every operation."""

import hashlib
import json
import sqlite3
import uuid
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

from .credentials import validate_credentials
from . import activity, policies, access


class CRMError(Exception):
    def __init__(self, status_code, detail):
        super().__init__(detail)
        self.status_code = status_code
        self.detail = detail


SAFE_DETAILS = {
    "Sensitive content blocked",
    "Access temporarily throttled",
    "Request budget exceeded", "Record budget exceeded", "Bulk response blocked", "Policy state unavailable",
    "Principal disabled", "Credential revoked or expired", "Principal not found", "Access state changed", "Invalid access transition", "Revoked credentials cannot be restored",
    "Identity context unavailable", "Inactive owner or employee", "Invalid identity projection", "Identity projection is stale or replayed",
    "Missing or invalid bearer token", "Administrator credential required",
    "No active grant permits this operation", "Customer not found", "Grant not found",
    "Only notes and subscription_status can be updated", "Notes must be text of at most 500 characters",
    "Invalid subscription status", "Customer changed; fetch current version before editing",
    "Grant changed; fetch current version before revocation", "Operation failed",
}


def token_hash(token):
    return hashlib.sha256(token.encode()).hexdigest()


class CRMStore:
    def __init__(self, path: Path, *, activity_enabled=False, activity_limit=10000, policy_config=None, clock=time.time):
        if type(activity_enabled) is not bool:
            raise ValueError("activity_enabled must be a boolean")
        if type(activity_limit) is not int or not 1 <= activity_limit <= 1000000:
            raise ValueError("activity_limit must be an integer between 1 and 1000000")
        self.policy_config = policy_config or policies.PolicyConfig()
        self.clock = clock
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
                      "agent": "agent:sales-research", "observer": "service:security-observer",
                      "recovery": "service:activity-reader", "publisher": "service:identity-publisher",
                      "actions": "service:awasi-actions"}
        if self.is_initialized():
            with self.connection() as db:
                db.execute("BEGIN IMMEDIATE")
                version = db.execute("SELECT value FROM metadata WHERE key='schema_version'").fetchone()[0]
                if version not in {"1", "2", "3", "4"}:
                    raise ValueError("Unsupported CRM schema version")
                existing = {r["principal_id"]: r["token_hash"] for r in db.execute("SELECT * FROM credentials")}
                expected = {principals[k]: token_hash(v) for k, v in tokens.items()}
                if any(expected.get(k) != v for k, v in existing.items()) or any(
                    k not in existing for k in expected if k not in {"service:security-observer", "service:activity-reader", "service:identity-publisher", "service:awasi-actions"}
                ):
                    raise ValueError("Configured credentials do not match stored credentials; restore the original .env")
                if version == "1":
                    db.execute("ALTER TABLE grants ADD COLUMN resource_id TEXT NOT NULL DEFAULT 'customer-database'")
                    db.execute("UPDATE metadata SET value='2' WHERE key='schema_version'")
                if "observer" in tokens and "service:security-observer" not in existing:
                    self._add_observer(db, tokens["observer"])
                if "recovery" in tokens and "service:activity-reader" not in existing:
                    self._add_recovery_reader(db, tokens["recovery"])
                if version in {"1", "2"}:
                    activity.initialize(db)
                    db.execute("UPDATE metadata SET value='3' WHERE key='schema_version'")
                activity.validate_schema(db, delivery=version == '4')
                if version != '4':
                    activity.migrate_outbox(db)
                    db.execute("UPDATE metadata SET value='4' WHERE key='schema_version'")
                policies.initialize(db)
                if "publisher" in tokens and "service:identity-publisher" not in existing:
                    self._add_publisher(db, tokens["publisher"])
                if "actions" in tokens and "service:awasi-actions" not in existing:
                    self._add_actions(db, tokens["actions"])
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
                           [(token_hash(token), principals[key]) for key, token in tokens.items() if key not in {"observer", "recovery", "publisher", "actions"}])
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
            if "recovery" in tokens:
                self._add_recovery_reader(db, tokens["recovery"])
            if "actions" in tokens:
                self._add_actions(db, tokens["actions"])
            activity.initialize(db)
            activity.migrate_outbox(db)
            db.execute("INSERT INTO metadata VALUES ('schema_version', '4')")
            policies.initialize(db)
            if "publisher" in tokens:
                self._add_publisher(db, tokens["publisher"])

    @staticmethod
    def _add_publisher(db, token):
        db.execute("INSERT INTO principals VALUES ('service:identity-publisher','Identity Publisher','service','emp-casey-wong','user')")
        db.execute("INSERT INTO credentials VALUES (?,'service:identity-publisher')", (token_hash(token),))
        db.execute("INSERT INTO grants (id,principal_id,scopes,status,version,resource_id) VALUES ('grant-identity-publisher','service:identity-publisher',?,'active',1,'crm-identity')", (json.dumps(['identity:publish']),))

    @staticmethod
    def _add_recovery_reader(db, token):
        db.execute("INSERT INTO principals VALUES ('service:activity-reader', 'Activity Reader', 'service', 'emp-casey-wong', 'user')")
        db.execute("INSERT INTO credentials VALUES (?, 'service:activity-reader')", (token_hash(token),))
        db.execute("INSERT INTO grants (id,principal_id,scopes,status,version,resource_id) VALUES ('grant-activity-reader', 'service:activity-reader', ?, 'active', 1, 'crm-activity')",
                   (json.dumps(['events:read']),))

    @staticmethod
    def _add_observer(db, token):
        db.execute("INSERT INTO principals VALUES ('service:security-observer', 'Security Observer', 'service', 'emp-casey-wong', 'user')")
        db.execute("INSERT INTO credentials VALUES (?, 'service:security-observer')", (token_hash(token),))
        db.execute("""INSERT INTO grants (id,principal_id,scopes,status,version,resource_id) VALUES ('grant-security-observer', 'service:security-observer',
                   ?, 'active', 1, 'crm-metadata')""",
                   (json.dumps(["accounts:read", "grants:read", "inventory:read"]),))

    @staticmethod
    def _add_actions(db, token):
        db.execute("INSERT INTO principals VALUES ('service:awasi-actions', 'Awasi Actions', 'service', 'emp-casey-wong', 'user')")
        db.execute("INSERT INTO credentials VALUES (?, 'service:awasi-actions')", (token_hash(token),))
        db.execute("""INSERT INTO grants (id,principal_id,scopes,status,version,resource_id)
                   VALUES ('grant-awasi-actions', 'service:awasi-actions', ?, 'active', 1, 'crm-metadata')""",
                   (json.dumps(["accounts:read", "grants:read", "access:change", "grants:revoke"]),))

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

    def _perform(self, token, action, resource, operation, *, admin=False, metadata=False, grant_resource=None):
        error, result, actor = None, None, None
        decisions = []
        budget_attempt, prepared_count, record_ids = False, 0, []
        canonical_resource = activity.RESOURCES[action]
        with self._operation_connection() as db:
            policy_now = self.clock()
            # Serialize authorization with mutations: revocation takes effect before
            # the next operation's permission check, with no check/use race.
            principal = db.execute("""SELECT p.* FROM credentials c JOIN principals p
                ON c.principal_id=p.id WHERE c.token_hash=?""", (token_hash(token or ""),)).fetchone()
            actor = principal['id'] if principal else None
            previous_clock = db.execute("SELECT value FROM metadata WHERE key='access_clock'").fetchone()
            access_now = max(policy_now,float(previous_clock[0]) if previous_clock else policy_now)
            if principal is not None:
                db.execute("INSERT OR REPLACE INTO metadata VALUES ('access_clock',?)",(str(access_now),))
            failure_key = 'failure:' + (principal['id'] if principal else token_hash(token or ''))
            before = activity.record_snapshot(db, action, resource)
            db.execute("SAVEPOINT crm_operation")
            try:
                if self.policy_config.failure_mode != 'off':
                    check = policies.failure_check(db,failure_key,self.policy_config,policy_now)
                    if check:
                        decisions.append(check)
                        self._require(check)
                if principal is None:
                    raise CRMError(401, "Missing or invalid bearer token")
                actor = principal["id"]
                access_state = access.state(db, actor)
                if access_state['principal_status'] == 'disabled':
                    decisions.append(policies.decision('credential_lifecycle','deny','principal_disabled'))
                    raise CRMError(403,'Principal disabled')
                if access_state['credential_status'] == 'revoked' or (access_state['expires_at'] is not None and access_now >= access_state['expires_at']):
                    decisions.append(policies.decision('credential_lifecycle','deny','credential_invalid'))
                    raise CRMError(401,'Credential revoked or expired')
                if admin:
                    # Allow either the built-in admin role or an active grant covering the action scope.
                    has_grant = any(
                        action in json.loads(row["scopes"])
                        for row in db.execute(
                            "SELECT scopes FROM grants WHERE principal_id=? AND status='active' "
                            "AND resource_id=? AND app_id='helio-crm'",
                            (actor, canonical_resource),
                        )
                    )
                    if principal["role"] != "admin" and not has_grant:
                        raise CRMError(403, "Administrator credential required")
                elif not (metadata and principal["role"] == "admin"):
                    scopes = {scope for row in db.execute(
                        "SELECT scopes FROM grants WHERE principal_id=? AND status='active' AND resource_id=? AND app_id='helio-crm'",
                        (actor, canonical_resource)
                    ) for scope in json.loads(row["scopes"])}
                    if action not in scopes:
                        raise CRMError(403, "No active grant permits this operation")
                decisions.append(policies.decision('resource_permissions', 'allow', 'authorized'))
                if canonical_resource == 'customer-database':
                    check = policies.owner_check(db, principal, self.policy_config, access_now)
                    if check:
                        decisions.append(check)
                        if check['mode'] == 'enforce' and check['result'] in {'deny','unknown'}:
                            raise CRMError(503 if check['result']=='unknown' else 403,
                                           'Identity context unavailable' if check['result']=='unknown' else 'Inactive owner or employee')
                if action == 'customers:read' and self.policy_config.volume_mode != 'off':
                    budget_attempt = True
                    check = policies.budget_check(db,actor,None,self.policy_config,policy_now)
                    if check['result'] != 'allow':
                        decisions.append(check)
                        self._require(check)
                result = operation(db)
                if action == 'customers:read':
                    rows = result if isinstance(result,list) else [result]
                    prepared_count = len(rows)
                    record_ids = [r['id'] for r in rows[:50]]
                if canonical_resource == 'customer-database' and self.policy_config.content_mode != 'off':
                    checks = policies.content_checks(result,self.policy_config,'pre_commit' if action=='customers:write' else 'pre_release')
                    decisions.extend(checks)
                    for check in checks: self._require(check)
                if budget_attempt:
                    prepared_count = len(result) if isinstance(result,list) else 1
                    check = policies.budget_check(db,actor,prepared_count,self.policy_config,policy_now)
                    decisions.append(check)
                    self._require(check)
            except CRMError as caught:
                db.execute("ROLLBACK TO crm_operation")
                result = None
                if caught.status_code in (401,403) and not decisions:
                    decisions.append(policies.decision('resource_permissions', 'deny', 'authentication_failed' if caught.status_code == 401 else 'access_denied'))
                error = CRMError(caught.status_code, caught.detail if caught.detail in SAFE_DETAILS else "Operation failed")
            except sqlite3.Error:
                raise  # Storage failure rolls back the entire operation and audit.
            except Exception:
                db.execute("ROLLBACK TO crm_operation")
                error = CRMError(500, "Operation failed")
            db.execute("RELEASE crm_operation")
            status = error.status_code if error else 200
            outcome = "allowed" if not error else ("denied" if status in (401, 403, 429) else "failed")
            occurred_at, request_id = datetime.fromtimestamp(self.clock(), timezone.utc).isoformat(), str(uuid.uuid4())
            from .lifecycle import context
            if context.get() is not None:
                request_id = context.get()['request_id']
            if self.policy_config.failure_mode != 'off':
                failed = error is not None and error.detail in {'Missing or invalid bearer token','No active grant permits this operation','Administrator credential required','Credential revoked or expired'}
                authenticated = any(d['rule']=='resource_permissions' and d['result']=='allow' for d in decisions)
                check = policies.record_failure(db,failure_key,request_id,failed,authenticated,self.policy_config,policy_now)
                if check: decisions.append(check)
            if budget_attempt:
                policies.record_budget(db,request_id,actor,prepared_count,error is None,self.policy_config,policy_now)
            policies.persist(db, decisions, request_id, actor, canonical_resource, datetime.fromisoformat(occurred_at).timestamp())
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
                                limit=self.activity_limit, decisions=decisions, prepared_count=prepared_count, record_ids=record_ids)
        # Raise only after committing the denial record.
        if error:
            raise error
        return result

    @staticmethod
    def _require(check):
        if check['mode'] != 'enforce' or check['result'] not in {'deny','unknown'}:
            return
        if check['result'] == 'unknown':
            raise CRMError(503,'Policy state unavailable')
        if check['rule'] == 'sensitive_content':
            raise CRMError(403,'Sensitive content blocked')
        status, detail = {'access_cooldown':(429,'Access temporarily throttled'),
                          'request_budget':(429,'Request budget exceeded'),
                          'record_budget':(429,'Record budget exceeded'),
                          'bulk_result':(403,'Bulk response blocked')}.get(check['reason'],(403,'Operation failed'))
        raise CRMError(status,detail)

    def change_access(self, token, principal_id, expected_version, **changes):
        return self._perform(token,'access:change',principal_id,
                             lambda db: access.change(db,principal_id,expected_version,self.clock(),**changes),admin=True)

    def publish_identity(self, token, payload):
        from .identity import publish
        return self._perform(token,'identity:publish','crm-identity',lambda db: publish(db,payload,self.clock()))

    def activity_feed(self, token, *, status_only=False, after=0, through=None, source_instance_id=None, limit=100):
        def read(db):
            epoch = db.execute("SELECT value FROM metadata WHERE key='activity_source_instance_id'").fetchone()[0]
            high = db.execute("SELECT seq FROM sqlite_sequence WHERE name='activity_events'").fetchone()
            high = high[0] if high else 0
            earliest = db.execute('SELECT min(sequence) FROM activity_events').fetchone()[0] or high + 1
            queue = dict.fromkeys(('pending', 'inflight', 'delivered', 'dead_letter'), 0)
            queue.update(dict(db.execute('SELECT status,count(*) FROM activity_outbox GROUP BY status')))
            result = {'schema_version':1, 'company_id':'helio', 'source':'helio-crm',
                      'source_instance_id':epoch, 'high_watermark':high, 'earliest_available':earliest,
                      'capture_enabled':self.activity_enabled, 'queue':queue,
                      'retained_events':db.execute('SELECT count(*) FROM activity_events').fetchone()[0],
                      'capacity':self.activity_limit}
            if status_only:
                return result
            if source_instance_id is not None and source_instance_id != epoch:
                raise CRMError(409, 'Activity source changed')
            if after < earliest - 1:
                raise CRMError(410, 'Activity history expired')
            watermark = high if through is None else through
            if not 0 <= after <= watermark <= high or not 1 <= limit <= 200:
                raise CRMError(422, 'Invalid activity cursor')
            rows = db.execute('SELECT * FROM activity_events WHERE sequence>? AND sequence<=? ORDER BY sequence LIMIT ?',
                              (after, watermark, limit)).fetchall()
            items = [{**json.loads(row['payload']), **{k:row[k] for k in row.keys() if k != 'payload'}} for row in rows]
            cursor = items[-1]['sequence'] if items else after
            return {**result, 'through':watermark, 'items':items, 'next_after':cursor, 'has_more':cursor < watermark}
        return self._perform(token, 'events:read', 'crm-activity', read, grant_resource='crm-activity')

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
        return [{**dict(r), "scopes": json.loads(r["scopes"])} for r in db.execute("""
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

    @staticmethod
    def _accounts(db):
        return [dict(row) for row in db.execute("""SELECT p.*,
            coalesce(a.principal_status,'active') AS principal_status,
            coalesce(a.credential_status,'active') AS credential_status,
            a.expires_at AS credential_expires_at,coalesce(a.version,1) AS access_version
            FROM principals p LEFT JOIN access_state a ON a.principal_id=p.id ORDER BY p.id""")]

    def list_accounts(self, token):
        return self._perform(token, "accounts:read", "accounts",
                             self._accounts, metadata=True)

    def inventory(self, token):
        def snapshot(db):
            record_count = db.execute("SELECT count(*) FROM customers").fetchone()[0]
            resources = [
                {"id": "customer-database", "name": "Customer database", "classification": "confidential", "record_count": record_count},
                {"id": "crm-metadata", "name": "CRM access metadata", "classification": "internal"}]
            if db.execute("SELECT 1 FROM principals WHERE id='service:activity-reader'").fetchone():
                resources.append({"id":"crm-activity", "name":"CRM security activity", "classification":"internal"})
            if db.execute("SELECT 1 FROM principals WHERE id='service:identity-publisher'").fetchone():
                resources.append({"id":"crm-identity", "name":"Directory projection", "classification":"internal"})
            return {"schema_version": 1, "company_id": "helio", "complete": True,
                    "policy_configuration": self.policy_config.view(),
                    "app": {"id": "helio-crm", "name": "Helio CRM", "owner_employee_id": "emp-casey-wong"},
                    "resources": resources,
                    "accounts": self._accounts(db),
                    "grants": self._grants(db)}
        return self._perform(token, "inventory:read", "inventory", snapshot, metadata=True)

    def audit(self, token, limit=100):
        return self._perform(token, "audit:read", "audit",
                             lambda db: [dict(r) for r in db.execute(
                                 "SELECT * FROM audit ORDER BY id DESC LIMIT ?", (limit,))], admin=True)

    def replay_delivery(self, token, sequence):
        def replay(db):
            row = db.execute('SELECT status FROM activity_outbox WHERE event_sequence=?',(sequence,)).fetchone()
            if row is None: raise CRMError(404, 'Delivery not found')
            if row['status'] != 'dead_letter': raise CRMError(409, 'Delivery is not replayable')
            db.execute("""UPDATE activity_outbox SET status='pending',attempts=0,next_attempt_at=0,
                lease_id=NULL,lease_until=NULL,last_error=NULL WHERE event_sequence=?""",(sequence,))
            actor = db.execute('SELECT principal_id FROM credentials WHERE token_hash=?',(token_hash(token),)).fetchone()[0]
            db.execute("""INSERT INTO delivery_audit(event_sequence,occurred_at,status_code,outcome,actor_id)
                VALUES (?,?,200,'replayed',?)""",(sequence,datetime.now(timezone.utc).timestamp(),actor))
            db.execute('DELETE FROM delivery_audit WHERE id NOT IN (SELECT id FROM delivery_audit ORDER BY id DESC LIMIT 1000)')
            return {'event_sequence':sequence,'status':'pending'}
        return self._perform(token,'activity:replay','activity',replay,admin=True)
