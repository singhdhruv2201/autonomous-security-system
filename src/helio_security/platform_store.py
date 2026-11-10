"""Durable platform records, separate from customer data and graph projection."""

import json
import sqlite3
import time
import uuid
from contextlib import contextmanager
from pathlib import Path

from .rules import RULE_ID
from .policies import Policy, policy


class PlatformStore:
    def __init__(self, path):
        self.path = Path(path)

    @contextmanager
    def connection(self):
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys=ON")
        try:
            with db:
                yield db
        finally:
            db.close()

    def initialize(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connection() as db:
            db.execute("PRAGMA journal_mode=WAL")
            db.execute("BEGIN IMMEDIATE")
            db.execute("CREATE TABLE IF NOT EXISTS metadata (key TEXT PRIMARY KEY NOT NULL, value TEXT NOT NULL)")
            version = db.execute("SELECT value FROM metadata WHERE key='schema_version'").fetchone()
            if version and version[0] not in {"1", "2"}:
                raise ValueError("Unsupported platform schema version")
            for statement in [
                """CREATE TABLE IF NOT EXISTS runs (id TEXT PRIMARY KEY NOT NULL, company_id TEXT NOT NULL,
                    actor TEXT NOT NULL, trigger TEXT NOT NULL, status TEXT NOT NULL,
                    created_at REAL NOT NULL, started_at REAL, finished_at REAL,
                    source_results TEXT NOT NULL DEFAULT '{}', error TEXT)""",
                """CREATE UNIQUE INDEX IF NOT EXISTS one_active_run ON runs(company_id)
                    WHERE status IN ('queued','running')""",
                """CREATE TABLE IF NOT EXISTS findings (id TEXT PRIMARY KEY NOT NULL, company_id TEXT NOT NULL,
                    rule TEXT NOT NULL, severity TEXT NOT NULL, status TEXT NOT NULL, assessment TEXT NOT NULL,
                    first_seen REAL NOT NULL, last_seen REAL NOT NULL, last_evaluated REAL NOT NULL,
                    last_verified REAL NOT NULL, resolved_at REAL, evidence TEXT NOT NULL, reason TEXT NOT NULL)""",
                """CREATE TABLE IF NOT EXISTS events (id INTEGER PRIMARY KEY AUTOINCREMENT,
                    company_id TEXT NOT NULL, finding_id TEXT NOT NULL REFERENCES findings(id),
                    type TEXT NOT NULL, occurred_at REAL NOT NULL, run_id TEXT NOT NULL REFERENCES runs(id),
                    reason TEXT NOT NULL, evidence TEXT NOT NULL)""",
                """CREATE TABLE IF NOT EXISTS audit (id INTEGER PRIMARY KEY AUTOINCREMENT,
                    company_id TEXT NOT NULL, actor TEXT NOT NULL, action TEXT NOT NULL,
                    run_id TEXT NOT NULL REFERENCES runs(id), occurred_at REAL NOT NULL)""",
                "CREATE TABLE IF NOT EXISTS evaluations (company_id TEXT PRIMARY KEY NOT NULL, observed_at REAL NOT NULL)",
                "CREATE INDEX IF NOT EXISTS finding_company ON findings(company_id, status)",
            ]:
                db.execute(statement)
            db.execute("CREATE TABLE IF NOT EXISTS rule_evaluations (company_id TEXT NOT NULL, rule TEXT NOT NULL, observed_at REAL NOT NULL, PRIMARY KEY(company_id,rule))")
            db.execute("INSERT OR IGNORE INTO rule_evaluations SELECT company_id,?,observed_at FROM evaluations", (RULE_ID,))
            if next(r for r in db.execute('PRAGMA table_info(events)') if r['name']=='run_id')['notnull']:
                db.execute("CREATE TABLE events_v2 (id INTEGER PRIMARY KEY AUTOINCREMENT,company_id TEXT NOT NULL,finding_id TEXT NOT NULL REFERENCES findings(id),type TEXT NOT NULL,occurred_at REAL NOT NULL,run_id TEXT REFERENCES runs(id),reason TEXT NOT NULL,evidence TEXT NOT NULL)")
                db.execute('INSERT INTO events_v2 SELECT * FROM events')
                db.execute('DROP TABLE events')
                db.execute('ALTER TABLE events_v2 RENAME TO events')
            db.execute('CREATE TABLE IF NOT EXISTS activity_import_state (company_id TEXT PRIMARY KEY,instance_id TEXT NOT NULL,cursor INTEGER NOT NULL,last_success REAL NOT NULL)')
            db.execute('CREATE TABLE IF NOT EXISTS policy_runtime (company_id TEXT NOT NULL,name TEXT NOT NULL,value TEXT NOT NULL,updated_at REAL NOT NULL,PRIMARY KEY(company_id,name))')
            db.execute("INSERT OR REPLACE INTO metadata VALUES ('schema_version','2')")

    @staticmethod
    def _run(row):
        if row is None:
            return None
        result = dict(row)
        result["source_results"] = json.loads(result["source_results"])
        return result

    def enqueue(self, company, actor, trigger, now):
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            active = db.execute("SELECT * FROM runs WHERE company_id=? AND status IN ('queued','running')", (company,)).fetchone()
            if active:
                return self._run(active), False
            run_id = str(uuid.uuid4())
            db.execute("INSERT INTO runs(id,company_id,actor,trigger,status,created_at) VALUES (?,?,?,?,'queued',?)",
                       (run_id, company, actor, trigger, now))
            db.execute("INSERT INTO audit(company_id,actor,action,run_id,occurred_at) VALUES (?,?,'sync_requested',?,?)",
                       (company, actor, run_id, now))
            return self._run(db.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone()), True

    def claim(self, company, now):
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT * FROM runs WHERE company_id=? AND status='queued'", (company,)).fetchone()
            if row is None:
                return None
            db.execute("UPDATE runs SET status='running',started_at=? WHERE id=?", (now, row["id"]))
            return self._run(db.execute("SELECT * FROM runs WHERE id=?", (row["id"],)).fetchone())

    def recover(self, company, now):
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            runs = list(db.execute("SELECT * FROM runs WHERE company_id=? AND status='running'", (company,)))
            for run in runs:
                db.execute("INSERT INTO audit(company_id,actor,action,run_id,occurred_at) VALUES (?,?,'sync_interrupted',?,?)",
                           (company, run["actor"], run["id"], now))
                for previous in list(db.execute("SELECT * FROM findings WHERE company_id=? AND rule=?", (company, RULE_ID))):
                    self._unverify(db, company, previous, run["id"], now, "observation_interrupted")
            db.execute("UPDATE runs SET status='interrupted',finished_at=?,error='process_restarted' WHERE company_id=? AND status='running'",
                       (now, company))

    @staticmethod
    def _event(db, company, finding_id, event, now, run_id, reason):
        evidence = db.execute("SELECT evidence FROM findings WHERE id=? AND company_id=?", (finding_id, company)).fetchone()[0]
        db.execute("INSERT INTO events(company_id,finding_id,type,occurred_at,run_id,reason,evidence) VALUES (?,?,?,?,?,?,?)",
                   (company, finding_id, event, now, run_id, reason, evidence))

    def _reconcile(self, db, company, run_id, evaluation, now):
        rule = evaluation.get("rule", RULE_ID)
        definition = policy(rule)
        old = db.execute("SELECT observed_at FROM rule_evaluations WHERE company_id=? AND rule=?", (company, rule)).fetchone()
        if old and evaluation["observed_at"] <= old[0]:
            return
        db.execute("INSERT INTO rule_evaluations VALUES (?,?,?) ON CONFLICT(company_id,rule) DO UPDATE SET observed_at=excluded.observed_at",
                   (company, rule, evaluation["observed_at"]))
        existing = {r["id"]: dict(r) for r in db.execute("SELECT * FROM findings WHERE company_id=? AND rule=?", (company, rule))}
        seen = set()
        for candidate in evaluation["candidates"]:
            identity = json.dumps([company, rule, *candidate["identity"]])
            finding_id = str(uuid.uuid5(uuid.NAMESPACE_URL, identity))
            seen.add(finding_id)
            previous = existing.get(finding_id)
            state = candidate["state"] if evaluation["coverage_complete"] else "unknown"
            reason = candidate["reason"] if evaluation["coverage_complete"] else "incomplete_coverage"
            if state == "unknown":
                if previous:
                    self._unverify(db, company, previous, run_id, now, reason)
                continue
            if previous is None and state != "open":
                continue
            serialized = json.dumps(candidate["evidence"], sort_keys=True)
            # Use actual source observation time, not evaluation time, for freshness.
            verified = (min(evaluation["sources"][s]["last_success"] for s in definition.sources)
                        if definition.lifecycle == "posture" else evaluation["observed_at"])
            if previous is None:
                db.execute("INSERT INTO findings VALUES (?,?,?,'high','open','current',?,?,?,?,NULL,?,?)",
                    (finding_id, company, rule, now, now, now, verified, serialized, reason))
                event = "opened"
            else:
                event = None
                if previous["status"] != state:
                    event = "resolved" if state == "resolved" else "reopened"
                elif previous["assessment"] == "unknown":
                    event = "verified"
                else:
                    old_evidence = json.loads(previous["evidence"])
                    stable = lambda value: {k: v for k, v in value.items() if not k.endswith("_observed_at")}
                    if stable(old_evidence) != stable(candidate["evidence"]):
                        event = "evidence_changed"
                resolved_at = (previous["resolved_at"] or now) if state == "resolved" else None
                db.execute("""UPDATE findings SET status=?,assessment='current',last_seen=?,last_evaluated=?,
                    last_verified=?,resolved_at=?,evidence=?,reason=? WHERE id=? AND company_id=?""",
                    (state, now if state == "open" else previous["last_seen"], now, verified,
                     resolved_at, serialized, reason, finding_id, company))
            if event:
                self._event(db, company, finding_id, event, now, run_id, reason)
        for finding_id, previous in existing.items():
            if finding_id not in seen and definition.lifecycle == "posture":
                self._unverify(db, company, previous, run_id, now,
                               "missing_evidence" if evaluation["coverage_complete"] else "incomplete_coverage")

    def _unverify(self, db, company, previous, run_id, now, reason):
        db.execute("UPDATE findings SET assessment='unknown',last_evaluated=?,reason=? WHERE id=? AND company_id=?",
                   (now, reason, previous["id"], company))
        if previous["assessment"] != "unknown":
            self._event(db, company, previous["id"], "unverified", now, run_id, reason)

    def finish(self, company, run_id, source_results, evaluation, now, error=None):
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            run = db.execute("SELECT * FROM runs WHERE id=? AND company_id=? AND status='running'", (run_id, company)).fetchone()
            if not run:
                raise ValueError("Run is not active for this company")
            if error:
                error = "observation_failed"
            if evaluation is None:
                evaluation = {"coverage_complete": False, "observed_at": now, "candidates": [], "sources": {}}
            self._reconcile(db, company, run_id, evaluation, now)
            state = "failed" if error else ("succeeded" if evaluation["coverage_complete"] and
                    all(source_results.get(s) == "ok" for s in ("directory", "crm")) else "partial")
            db.execute("UPDATE runs SET status=?,finished_at=?,source_results=?,error=? WHERE id=?",
                       (state, now, json.dumps(source_results), error, run_id))
            db.execute("INSERT INTO audit(company_id,actor,action,run_id,occurred_at) VALUES (?,?,'sync_finished',?,?)",
                       (company, run["actor"], run_id, now))

    @staticmethod
    def _finding(row, now):
        result = dict(row)
        result["evidence"] = json.loads(result["evidence"])
        try:
            definition = policy(result["rule"])
        except ValueError:
            # Retired rules remain readable on existing databases, not evaluable.
            definition = Policy(result["rule"], f"Historical CRM policy: {result['rule']}",
                                "Retained evidence from a retired CRM policy.")
        result["lifecycle"] = definition.lifecycle
        result["rule_version"] = definition.version
        result["evidence_fresh"] = result["assessment"] == "current" and (definition.lifecycle == "incident" or 0 <= now - result["last_verified"] <= 900)
        if not result["evidence_fresh"]:
            result["assessment"] = "unknown"
        result["title"] = ("Inactive employee owns an agent with confidential data access" if result["rule"] == RULE_ID else definition.name)
        result["suggested_fix"] = definition.suggested_fix
        return result

    def findings(self, company, *, status=None, limit=50, offset=0, now=None):
        now = time.time() if now is None else now
        where = "company_id=?" + (" AND status=?" if status else "")
        params = [company] + ([status] if status else [])
        with self.connection() as db:
            count = db.execute(f"SELECT count(*) FROM findings WHERE {where}", params).fetchone()[0]
            rows = db.execute(f"SELECT * FROM findings WHERE {where} ORDER BY first_seen DESC,id LIMIT ? OFFSET ?", [*params, limit, offset])
            return {"items": [self._finding(r, now) for r in rows], "total": count, "limit": limit, "offset": offset}

    def finding(self, company, finding_id, *, now=None):
        with self.connection() as db:
            row = db.execute("SELECT * FROM findings WHERE company_id=? AND id=?", (company, finding_id)).fetchone()
            if row is None:
                return None
            result = self._finding(row, time.time() if now is None else now)
            events = db.execute("SELECT * FROM events WHERE company_id=? AND finding_id=? ORDER BY id DESC LIMIT 100", (company, finding_id))
            result["events"] = list(reversed([{**dict(e), "evidence": json.loads(e["evidence"])} for e in events]))
            result["event_count"] = db.execute("SELECT count(*) FROM events WHERE company_id=? AND finding_id=?", (company, finding_id)).fetchone()[0]
            return result

    def run(self, company, run_id):
        with self.connection() as db:
            return self._run(db.execute("SELECT * FROM runs WHERE company_id=? AND id=?", (company, run_id)).fetchone())

    def _list(self, table, company, limit, offset):
        with self.connection() as db:
            order = "created_at DESC,id" if table == "runs" else "id DESC"
            count = db.execute(f"SELECT count(*) FROM {table} WHERE company_id=?", (company,)).fetchone()[0]
            rows = db.execute(f"SELECT * FROM {table} WHERE company_id=? ORDER BY {order} LIMIT ? OFFSET ?", (company, limit, offset))
            return {"items": [self._run(r) if table == "runs" else dict(r) for r in rows],
                    "total": count, "limit": limit, "offset": offset}

    def runs(self, company, limit=50, offset=0):
        return self._list("runs", company, limit, offset)

    def audit(self, company, limit=50, offset=0):
        return self._list("audit", company, limit, offset)

    def set_runtime(self,company,name,value,now):
        with self.connection() as db:
            db.execute('INSERT OR REPLACE INTO policy_runtime VALUES (?,?,?,?)',(company,name,json.dumps(value),now))

    def runtime(self,company):
        with self.connection() as db:
            return {r['name']:{'value':json.loads(r['value']),'updated_at':r['updated_at']} for r in
                    db.execute('SELECT * FROM policy_runtime WHERE company_id=?',(company,))}
