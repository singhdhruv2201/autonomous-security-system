"""Durable case records on the same SQLite file as PlatformStore.

Tables are additive (CREATE TABLE IF NOT EXISTS) so both stores can
coexist on the same file without coordination.

Case lifecycle:
  investigating → (fixing | waiting_owner | waiting_approver)
               → verifying → (resolved | needs_review)
  Any state → dismissed
"""

import json
import sqlite3
import time
import uuid
from contextlib import contextmanager
from pathlib import Path

from .redaction import scrub_text, safe_identifier
from .notification_copy import clean_message


CASE_STATUSES = frozenset({
    "investigating", "waiting_owner", "waiting_approver",
    "fixing", "verifying", "resolved", "needs_review", "dismissed",
})

ACTION_STATUSES = frozenset({"pending", "executed", "failed", "unknown", "undone"})


class CaseStore:
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
            for statement in [
                # One non-closed case per finding is enforced by the unique partial index.
                """CREATE TABLE IF NOT EXISTS cases (
                    id TEXT PRIMARY KEY NOT NULL,
                    company_id TEXT NOT NULL,
                    finding_id TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'investigating',
                    route TEXT NOT NULL,
                    summary TEXT NOT NULL DEFAULT '',
                    addressed_to TEXT,
                    wait_until REAL,
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL,
                    closed_at REAL
                )""",
                """CREATE UNIQUE INDEX IF NOT EXISTS one_active_case_per_finding
                    ON cases(company_id, finding_id)
                    WHERE status NOT IN ('resolved','needs_review','dismissed')""",
                # Case timeline events. UNIQUE(case_id,key) makes re-runs idempotent.
                """CREATE TABLE IF NOT EXISTS case_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    case_id TEXT NOT NULL REFERENCES cases(id),
                    key TEXT NOT NULL,
                    type TEXT NOT NULL,
                    actor TEXT NOT NULL,
                    text TEXT NOT NULL DEFAULT '',
                    data TEXT NOT NULL DEFAULT '{}',
                    occurred_at REAL NOT NULL,
                    UNIQUE(case_id, key)
                )""",
                # Actions (playbook executions). UNIQUE idempotency_key makes re-runs safe.
                """CREATE TABLE IF NOT EXISTS case_actions (
                    id TEXT PRIMARY KEY NOT NULL,
                    case_id TEXT NOT NULL REFERENCES cases(id),
                    playbook TEXT NOT NULL,
                    target TEXT NOT NULL,
                    idempotency_key TEXT NOT NULL UNIQUE,
                    status TEXT NOT NULL DEFAULT 'pending',
                    before TEXT,
                    result TEXT,
                    requested_by TEXT NOT NULL,
                    created_at REAL NOT NULL,
                    dispatched_at REAL,
                    executed_at REAL,
                    verified_at REAL,
                    undo_of TEXT REFERENCES case_actions(id),
                    dispatch_version INTEGER NOT NULL DEFAULT 2
                )""",
                # Human decisions. UNIQUE(case_id,key); consumed_at set when the
                # worker reads and acts on the decision.
                """CREATE TABLE IF NOT EXISTS case_decisions (
                    id TEXT PRIMARY KEY NOT NULL,
                    case_id TEXT NOT NULL REFERENCES cases(id),
                    key TEXT NOT NULL,
                    employee_id TEXT NOT NULL,
                    source TEXT NOT NULL,
                    choice TEXT NOT NULL,
                    occurred_at REAL NOT NULL,
                    consumed_at REAL,
                    UNIQUE(case_id, key)
                )""",
                """CREATE TABLE IF NOT EXISTS case_questions (
                    case_id TEXT PRIMARY KEY REFERENCES cases(id),
                    key TEXT NOT NULL,
                    payload TEXT NOT NULL
                )""",
                """CREATE TABLE IF NOT EXISTS finding_overrides (
                    company_id TEXT NOT NULL,
                    finding_id TEXT NOT NULL,
                    case_id TEXT NOT NULL REFERENCES cases(id),
                    occurred_at REAL NOT NULL,
                    PRIMARY KEY(company_id, finding_id)
                )""",
                """CREATE TABLE IF NOT EXISTS trace_spans (
                    id TEXT PRIMARY KEY NOT NULL,
                    case_id TEXT NOT NULL REFERENCES cases(id),
                    parent_id TEXT,
                    kind TEXT NOT NULL,
                    name TEXT NOT NULL,
                    started_at REAL NOT NULL,
                    ended_at REAL NOT NULL,
                    input_chars INTEGER NOT NULL DEFAULT 0,
                    output_chars INTEGER NOT NULL DEFAULT 0,
                    tokens_in INTEGER,
                    tokens_out INTEGER,
                    error TEXT,
                    attributes TEXT NOT NULL DEFAULT '{}'
                )""",
                """CREATE INDEX IF NOT EXISTS idx_trace_spans_case ON trace_spans(case_id, started_at)""",
                """CREATE TABLE IF NOT EXISTS case_investigations (
                    case_id TEXT PRIMARY KEY NOT NULL REFERENCES cases(id),
                    summary TEXT NOT NULL,
                    impact TEXT NOT NULL,
                    likely_cause TEXT NOT NULL,
                    confidence TEXT NOT NULL,
                    question_for_owner TEXT,
                    evidence_refs TEXT NOT NULL DEFAULT '[]',
                    recommended_playbook TEXT,
                    source TEXT NOT NULL DEFAULT 'model',
                    raw_response TEXT NOT NULL DEFAULT '{}',
                    fallback_reason TEXT,
                    created_at REAL NOT NULL
                )""",
            ]:
                db.execute(statement)
            case_columns = {row["name"] for row in db.execute("PRAGMA table_info(cases)")}
            if "report_pending" not in case_columns:
                # Existing closed cases get one checkpoint inspection on upgrade.
                db.execute("ALTER TABLE cases ADD COLUMN report_pending INTEGER NOT NULL DEFAULT 1")
            columns = {row["name"] for row in db.execute("PRAGMA table_info(case_actions)")}
            if "dispatch_version" not in columns:
                db.execute("ALTER TABLE case_actions ADD COLUMN dispatch_version INTEGER NOT NULL DEFAULT 1")
            inv_columns = {row["name"] for row in db.execute("PRAGMA table_info(case_investigations)")}
            if "fallback_reason" not in inv_columns:
                db.execute("ALTER TABLE case_investigations ADD COLUMN fallback_reason TEXT")
            if "slack_message" not in inv_columns:
                db.execute("ALTER TABLE case_investigations ADD COLUMN slack_message TEXT")

    # ------------------------------------------------------------------
    # Cases
    # ------------------------------------------------------------------

    def open_case(self, company_id: str, finding_id: str, *, route: str, now: float,
                  summary: str = "", addressed_to: str | None = None) -> str:
        """Return the existing active case id, or create and return a new one."""
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            existing = db.execute(
                "SELECT id FROM cases WHERE company_id=? AND finding_id=? "
                "AND status NOT IN ('resolved','needs_review','dismissed')",
                (company_id, finding_id),
            ).fetchone()
            if existing:
                return existing["id"]
            case_id = str(uuid.uuid4())
            db.execute(
                "INSERT INTO cases(id,company_id,finding_id,route,summary,addressed_to,created_at,updated_at) "
                "VALUES (?,?,?,?,?,?,?,?)",
                (case_id, company_id, finding_id, route, summary, addressed_to, now, now),
            )
            return case_id

    def get_case(self, case_id: str) -> dict | None:
        with self.connection() as db:
            row = db.execute("SELECT * FROM cases WHERE id=?", (case_id,)).fetchone()
            return dict(row) if row else None

    def update_case_status(self, case_id: str, *, status: str, now: float,
                           addressed_to: str | None = None, summary: str | None = None,
                           wait_until: float | None = None):
        assert status in CASE_STATUSES, f"Unknown case status: {status}"
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            sets = ["status=?", "updated_at=?"]
            params: list = [status, now]
            if addressed_to is not None:
                sets.append("addressed_to=?"); params.append(addressed_to)
            if summary is not None:
                sets.append("summary=?"); params.append(summary)
            if wait_until is not None:
                sets.append("wait_until=?"); params.append(wait_until)
            params.append(case_id)
            db.execute(f"UPDATE cases SET {', '.join(sets)} WHERE id=?", params)

    def close_case(self, case_id: str, *, status: str, now: float):
        assert status in {"resolved", "needs_review", "dismissed"}
        with self.connection() as db:
            db.execute(
                "UPDATE cases SET status=?, closed_at=?, updated_at=?, wait_until=NULL, report_pending=1 WHERE id=?",
                (status, now, now, case_id),
            )

    def set_wait_until(self, case_id: str, wait_until: float):
        with self.connection() as db:
            db.execute("UPDATE cases SET wait_until=? WHERE id=?", (wait_until, case_id))

    def active_cases(self, company_id: str) -> list[dict]:
        with self.connection() as db:
            rows = db.execute(
                "SELECT * FROM cases WHERE company_id=? AND status NOT IN ('resolved','needs_review','dismissed') "
                "ORDER BY created_at",
                (company_id,),
            )
            return [dict(r) for r in rows]

    def recovery_cases(self, company_id: str) -> list[dict]:
        """Closed cases remain recoverable until their final checkpoint is complete."""
        with self.connection() as db:
            return [dict(row) for row in db.execute(
                "SELECT * FROM cases WHERE company_id=? AND "
                "(status NOT IN ('resolved','needs_review','dismissed') OR report_pending=1) "
                "ORDER BY created_at", (company_id,))]

    def mark_reported(self, case_id: str):
        """Reporting finished; this does not imply Slack delivered the outbox row."""
        with self.connection() as db:
            db.execute("UPDATE cases SET report_pending=0 WHERE id=? "
                       "AND status IN ('resolved','needs_review','dismissed')", (case_id,))

    def overdue_waiting_cases(self, company_id: str, *, now: float) -> list[dict]:
        with self.connection() as db:
            rows = db.execute(
                "SELECT * FROM cases WHERE company_id=? "
                "AND status NOT IN ('resolved','needs_review','dismissed') "
                "AND wait_until IS NOT NULL AND wait_until <= ? ORDER BY wait_until",
                (company_id, now),
            )
            return [dict(r) for r in rows]

    def open_findings_without_case(self, company_id: str, *, open_finding_ids: list[str]) -> list[str]:
        """Return finding IDs from the list that have no active case."""
        if not open_finding_ids:
            return []
        with self.connection() as db:
            placeholders = ",".join("?" * len(open_finding_ids))
            covered = {
                r["finding_id"]
                for r in db.execute(
                    f"SELECT finding_id FROM cases WHERE company_id=? AND finding_id IN ({placeholders}) "
                    "AND (status NOT IN ('resolved','needs_review','dismissed') OR "
                    "(status='needs_review' AND NOT EXISTS (SELECT 1 FROM finding_overrides o "
                    "WHERE o.company_id=cases.company_id AND o.finding_id=cases.finding_id AND o.case_id=cases.id)))",
                    [company_id, *open_finding_ids],
                )
            }
            return [fid for fid in open_finding_ids if fid not in covered]

    # ------------------------------------------------------------------
    # Events
    # ------------------------------------------------------------------

    def add_event(self, case_id: str, *, key: str, type: str, actor: str,
                  text: str, now: float, data: dict | None = None):
        """Insert or ignore (idempotent by key)."""
        with self.connection() as db:
            db.execute(
                "INSERT OR IGNORE INTO case_events(case_id,key,type,actor,text,data,occurred_at) "
                "VALUES (?,?,?,?,?,?,?)",
                (case_id, key, type, actor, text, json.dumps(data or {}), now),
            )

    def get_events(self, case_id: str) -> list[dict]:
        with self.connection() as db:
            rows = db.execute(
                "SELECT * FROM case_events WHERE case_id=? ORDER BY id",
                (case_id,),
            )
            return [{**dict(r), "data": json.loads(r["data"])} for r in rows]

    # ------------------------------------------------------------------
    # Actions
    # ------------------------------------------------------------------

    def record_action(self, case_id: str, *, playbook: str, target: str,
                      idempotency_key: str, requested_by: str, now: float,
                      undo_of: str | None = None) -> str:
        """Return action id; idempotent — returns existing id on duplicate key."""
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            existing = db.execute(
                "SELECT id FROM case_actions WHERE idempotency_key=?",
                (idempotency_key,),
            ).fetchone()
            if existing:
                return existing["id"]
            action_id = str(uuid.uuid4())
            db.execute(
                "INSERT INTO case_actions(id,case_id,playbook,target,idempotency_key,requested_by,created_at,undo_of,dispatch_version) "
                "VALUES (?,?,?,?,?,?,?,?,2)",
                (action_id, case_id, playbook, target, idempotency_key, requested_by, now, undo_of),
            )
            return action_id

    def update_action(self, action_id: str, *, status: str, now: float,
                      result: dict | None = None, before: dict | None = None,
                      dispatched_at: float | None = None, verified_at: float | None = None):
        assert status in ACTION_STATUSES
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            sets = ["status=?"]
            params: list = [status]
            if result is not None:
                sets.append("result=?"); params.append(json.dumps(result))
            if before is not None:
                sets.append("before=?"); params.append(json.dumps(before))
            if dispatched_at is not None:
                sets.append("dispatched_at=?"); params.append(dispatched_at)
            if status == "executed":
                sets.append("executed_at=?"); params.append(now)
            if verified_at is not None:
                sets.append("verified_at=?"); params.append(verified_at)
            params.append(action_id)
            db.execute(f"UPDATE case_actions SET {', '.join(sets)} WHERE id=?", params)

    def get_action(self, action_id: str) -> dict | None:
        with self.connection() as db:
            row = db.execute("SELECT * FROM case_actions WHERE id=?", (action_id,)).fetchone()
            if row is None:
                return None
            result = dict(row)
            result["result"] = json.loads(result["result"]) if result["result"] else None
            result["before"] = json.loads(result["before"]) if result["before"] else None
            return result

    def get_actions(self, case_id: str) -> list[dict]:
        with self.connection() as db:
            rows = db.execute(
                "SELECT * FROM case_actions WHERE case_id=? ORDER BY created_at",
                (case_id,),
            )
            results = []
            for row in rows:
                r = dict(row)
                r["result"] = json.loads(r["result"]) if r["result"] else None
                r["before"] = json.loads(r["before"]) if r["before"] else None
                results.append(r)
            return results

    # ------------------------------------------------------------------
    # Decisions
    # ------------------------------------------------------------------

    def offer_question(self, case_id: str, question: dict, *, now: float):
        payload = {k: question[k] for k in ("key", "role", "choices", "addressed_to", "deadline")}
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            # A replay cannot resurrect a question that already has an answer.
            if db.execute("SELECT 1 FROM case_decisions WHERE case_id=? AND key=?",
                          (case_id, question["key"])).fetchone():
                return
            db.execute("INSERT INTO case_questions VALUES (?,?,?) ON CONFLICT(case_id) DO UPDATE "
                       "SET key=excluded.key,payload=excluded.payload", (case_id, question["key"], json.dumps(payload)))
            db.execute("UPDATE cases SET status=?,addressed_to=?,wait_until=?,updated_at=? WHERE id=?",
                       ("waiting_owner" if question["role"] == "owner" else "waiting_approver",
                        question["addressed_to"], question["deadline"], now, case_id))

    def question(self, case_id: str) -> dict | None:
        with self.connection() as db:
            row = db.execute("SELECT payload FROM case_questions WHERE case_id=?", (case_id,)).fetchone()
            return json.loads(row["payload"]) if row else None

    def decision(self, case_id: str, key: str) -> dict | None:
        with self.connection() as db:
            row = db.execute("SELECT * FROM case_decisions WHERE case_id=? AND key=?", (case_id, key)).fetchone()
            return dict(row) if row else None

    def submit_decision(self, company_id: str, case_id: str, *, key: str, choice: str,
                        employee_id: str, source: str, now: float, actor_id: str | None = None) -> bool:
        """Atomically authorize a human answer against the currently offered question."""
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            case = db.execute("SELECT * FROM cases WHERE id=? AND company_id=?", (case_id, company_id)).fetchone()
            row = db.execute("SELECT payload FROM case_questions WHERE case_id=?", (case_id,)).fetchone()
            if not case or not row or case["status"] not in ("waiting_owner", "waiting_approver"):
                raise ValueError("This case is not waiting for a decision")
            question = json.loads(row["payload"])
            if (key != question["key"] or choice not in question["choices"]
                    or employee_id not in ("operator", question["addressed_to"])):
                raise ValueError("This decision is not offered to you")
            existing = db.execute("SELECT * FROM case_decisions WHERE case_id=? AND key=?", (case_id, key)).fetchone()
            if existing:
                if existing["choice"] == choice and existing["employee_id"] == employee_id:
                    return False
                raise ValueError("This question already has a decision")
            if question["deadline"] is not None and now >= question["deadline"]:
                raise ValueError("This question has expired")
            db.execute("INSERT INTO case_decisions(id,case_id,key,employee_id,source,choice,occurred_at) VALUES (?,?,?,?,?,?,?)",
                       (str(uuid.uuid4()), case_id, key, employee_id, source, choice, now))
            db.execute("INSERT INTO case_events(case_id,key,type,actor,text,data,occurred_at) VALUES (?,?,?,?,?,?,?)",
                       (case_id, f"received:{key}", "decision_received", employee_id, "Human decision received",
                        json.dumps({"source": source, "actor_id": actor_id, "choice": choice}), now))
            return True

    def record_decision(self, case_id: str, *, key: str, employee_id: str,
                        source: str, choice: str, now: float):
        """Record a human decision; idempotent by (case_id, key)."""
        with self.connection() as db:
            db.execute(
                "INSERT OR IGNORE INTO case_decisions(id,case_id,key,employee_id,source,choice,occurred_at) "
                "VALUES (?,?,?,?,?,?,?)",
                (str(uuid.uuid4()), case_id, key, employee_id, source, choice, now),
            )

    def consume_decision(self, case_id: str, key: str) -> dict | None:
        """Mark a decision consumed and return it, or None if already consumed."""
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT * FROM case_decisions WHERE case_id=? AND key=? AND consumed_at IS NULL",
                (case_id, key),
            ).fetchone()
            if row is None:
                return None
            now = time.time()
            db.execute(
                "UPDATE case_decisions SET consumed_at=? WHERE id=?",
                (now, row["id"]),
            )
            return dict(row)

    def unconsumed_decisions(self, case_id: str) -> list[dict]:
        with self.connection() as db:
            rows = db.execute(
                "SELECT * FROM case_decisions WHERE case_id=? AND consumed_at IS NULL ORDER BY occurred_at",
                (case_id,),
            )
            return [dict(r) for r in rows]

    def cases_with_pending_decisions(self, company_id: str) -> list[dict]:
        """Include closed cases whose final decision still needs acknowledgment."""
        with self.connection() as db:
            rows = db.execute(
                """SELECT DISTINCT c.* FROM cases c
                   JOIN case_decisions d ON d.case_id=c.id
                   WHERE c.company_id=? AND d.consumed_at IS NULL
                   ORDER BY c.created_at""",
                (company_id,),
            )
            return [dict(r) for r in rows]

    def set_human_override(self, case_id: str, *, now: float):
        with self.connection() as db:
            db.execute("INSERT OR IGNORE INTO finding_overrides(company_id,finding_id,case_id,occurred_at) "
                       "SELECT company_id,finding_id,id,? FROM cases WHERE id=?", (now, case_id))

    def has_human_override(self, company_id: str, finding_id: str) -> bool:
        with self.connection() as db:
            return db.execute("SELECT 1 FROM finding_overrides WHERE company_id=? AND finding_id=?",
                              (company_id, finding_id)).fetchone() is not None

    # ------------------------------------------------------------------
    # Spans & Investigations
    # ------------------------------------------------------------------

    def record_span(self, span: dict):
        attrs = span.get("attributes", {})
        encoded_attrs = json.dumps(attrs, sort_keys=True) if isinstance(attrs, dict) else str(attrs)
        with self.connection() as db:
            db.execute(
                """INSERT OR REPLACE INTO trace_spans
                   (id, case_id, parent_id, kind, name, started_at, ended_at,
                    input_chars, output_chars, tokens_in, tokens_out, error, attributes)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    span["id"],
                    span["case_id"],
                    span.get("parent_id"),
                    span["kind"],
                    span["name"],
                    span["started_at"],
                    span["ended_at"],
                    span.get("input_chars", 0),
                    span.get("output_chars", 0),
                    span.get("tokens_in"),
                    span.get("tokens_out"),
                    span.get("error"),
                    encoded_attrs,
                ),
            )

    def get_spans(self, case_id: str, *, limit: int = 500, offset: int = 0) -> list[dict]:
        with self.connection() as db:
            rows = db.execute(
                "SELECT * FROM trace_spans WHERE case_id=? ORDER BY started_at ASC, id ASC LIMIT ? OFFSET ?",
                (case_id, limit, offset),
            ).fetchall()
            spans = []
            for r in rows:
                item = dict(r)
                try:
                    item["attributes"] = json.loads(item["attributes"])
                except Exception:
                    pass
                spans.append(item)
            return spans

    def save_investigation(self, case_id: str, assessment: dict, *, source: str = "model", now: float | None = None):
        t = time.time() if now is None else now
        assessment = dict(assessment)
        for key, limit in (("summary", 600), ("impact", 300), ("question_for_owner", 200)):
            if assessment.get(key) is not None:
                assessment[key] = scrub_text(assessment[key])[:limit]
        refs = assessment.get("evidence_refs", [])
        refs = [ref for ref in refs if safe_identifier(ref)][:32] if isinstance(refs, (list, tuple)) else []
        encoded_refs = json.dumps(refs)
        raw = "{}"  # Raw model responses are never persisted.
        fallback_reason = assessment.get("fallback_reason")
        with self.connection() as db:
            db.execute(
                """INSERT OR REPLACE INTO case_investigations
                   (case_id, summary, impact, likely_cause, confidence, question_for_owner,
                    evidence_refs, recommended_playbook, source, raw_response, fallback_reason, created_at, slack_message)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    case_id,
                    assessment.get("summary", ""),
                    assessment.get("impact", ""),
                    assessment.get("likely_cause", "unknown"),
                    assessment.get("confidence", "low"),
                    assessment.get("question_for_owner"),
                    encoded_refs,
                    assessment.get("recommended_playbook"),
                    source,
                    raw,
                    fallback_reason,
                    t,
                    json.dumps(clean_message(assessment.get("slack_message"))),
                ),
            )

    def get_investigation(self, case_id: str) -> dict | None:
        with self.connection() as db:
            row = db.execute("SELECT * FROM case_investigations WHERE case_id=?", (case_id,)).fetchone()
            if not row:
                return None
            item = dict(row)
            try:
                item["slack_message"] = clean_message(json.loads(item.get("slack_message") or "null"))
            except (ValueError, TypeError):
                item["slack_message"] = None
            try:
                item["evidence_refs"] = json.loads(item["evidence_refs"])
            except Exception:
                pass
            try:
                item["raw_response"] = json.loads(item["raw_response"])
            except Exception:
                pass
            return item

    # ------------------------------------------------------------------
    # API views
    # ------------------------------------------------------------------

    def get_case_full(self, case_id: str) -> dict | None:
        case = self.get_case(case_id)
        if case is None:
            return None
        case["events"] = self.get_events(case_id)
        case["actions"] = self.get_actions(case_id)
        case["question"] = self.question(case_id)
        case["spans"] = self.get_spans(case_id)
        case["investigation"] = self.get_investigation(case_id)
        return case

    def list_cases(self, company_id: str, *, limit: int = 50, offset: int = 0) -> dict:
        with self.connection() as db:
            total = db.execute(
                "SELECT count(*) FROM cases WHERE company_id=?", (company_id,)
            ).fetchone()[0]
            rows = db.execute(
                "SELECT * FROM cases WHERE company_id=? ORDER BY created_at DESC LIMIT ? OFFSET ?",
                (company_id, limit, offset),
            )
            return {"items": [dict(r) for r in rows], "total": total,
                    "limit": limit, "offset": offset}
