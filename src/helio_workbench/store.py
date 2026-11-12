"""Workbench SQLite storage for apps, agents, destinations, and event logging."""
from datetime import datetime, timezone, timedelta
import json
import os
from pathlib import Path
import sqlite3
from typing import Any, Dict, List, Optional


class ConflictError(Exception):
    def __init__(self, message: str, current_version: Optional[int] = None):
        super().__init__(message)
        self.current_version = current_version


class NotFoundError(Exception):
    pass


class WorkbenchStore:
    def __init__(self, db_path: Path):
        self.path = db_path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.initialize()

    def connection(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, timeout=10.0)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA busy_timeout = 10000")
        return conn

    def initialize(self):
        with self.connection() as db:
            db.execute(
                """CREATE TABLE IF NOT EXISTS metadata (
                    key TEXT PRIMARY KEY NOT NULL,
                    value TEXT NOT NULL
                )"""
            )
            db.execute(
                """CREATE TABLE IF NOT EXISTS apps (
                    id TEXT PRIMARY KEY NOT NULL,
                    name TEXT NOT NULL,
                    owner_employee_id TEXT NOT NULL,
                    built_with TEXT NOT NULL,
                    visibility TEXT NOT NULL CHECK(visibility IN ('private', 'company', 'public')),
                    slug TEXT NOT NULL UNIQUE,
                    crm_principal_id TEXT NOT NULL,
                    crm_principal_kind TEXT NOT NULL,
                    version INTEGER NOT NULL DEFAULT 1
                )"""
            )
            db.execute(
                """CREATE TABLE IF NOT EXISTS agents (
                    id TEXT PRIMARY KEY NOT NULL,
                    name TEXT NOT NULL,
                    owner_employee_id TEXT NOT NULL,
                    status TEXT NOT NULL CHECK(status IN ('active', 'paused')),
                    crm_principal_id TEXT NOT NULL,
                    crm_principal_kind TEXT NOT NULL,
                    version INTEGER NOT NULL DEFAULT 1
                )"""
            )
            db.execute(
                """CREATE TABLE IF NOT EXISTS destinations (
                    agent_id TEXT NOT NULL REFERENCES agents(id),
                    host TEXT NOT NULL,
                    url TEXT NOT NULL,
                    enabled INTEGER NOT NULL DEFAULT 1,
                    version INTEGER NOT NULL DEFAULT 1,
                    PRIMARY KEY (agent_id, host)
                )"""
            )
            db.execute(
                """CREATE TABLE IF NOT EXISTS events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    occurred_at TEXT NOT NULL,
                    kind TEXT NOT NULL CHECK(kind IN ('agent.send', 'app.view', 'admin.change')),
                    subject_id TEXT NOT NULL,
                    actor TEXT NOT NULL,
                    destination_host TEXT,
                    record_count INTEGER NOT NULL DEFAULT 0,
                    bytes INTEGER NOT NULL DEFAULT 0,
                    fields TEXT NOT NULL DEFAULT '[]',
                    outcome TEXT NOT NULL DEFAULT 'success'
                )"""
            )
            db.execute("CREATE INDEX IF NOT EXISTS idx_events_occurred ON events(occurred_at)")
            db.execute("CREATE INDEX IF NOT EXISTS idx_events_subject ON events(subject_id, kind)")

            # Seed if empty
            has_apps = db.execute("SELECT 1 FROM apps LIMIT 1").fetchone()
            if not has_apps:
                db.execute(
                    """INSERT INTO apps (id, name, owner_employee_id, built_with, visibility, slug, crm_principal_id, crm_principal_kind, version)
                       VALUES
                       ('app-renewal-tracker', 'Renewal Tracker', 'emp-luca-rossi', 'internal-tool', 'company', 'renewals', 'service:renewal-tracker', 'service', 1),
                       ('app-support-lookup', 'Support Lookup', 'emp-nina-desai', 'internal-tool', 'company', 'support', 'service:support-lookup', 'service', 1)"""
                )
                db.execute(
                    """INSERT INTO agents (id, name, owner_employee_id, status, crm_principal_id, crm_principal_kind, version)
                       VALUES
                       ('agent-lead-enrichment', 'Lead Enrichment Agent', 'emp-zara-ali', 'active', 'agent:lead-enrichment', 'agent', 1),
                       ('agent-renewal-digest', 'Renewal Digest Agent', 'emp-hannah-reed', 'active', 'agent:renewal-digest', 'agent', 1)"""
                )
                db.execute(
                    """INSERT INTO destinations (agent_id, host, url, enabled, version)
                       VALUES ('agent-renewal-digest', 'partner-api', 'http://partner-api:8005/collect', 1, 1)"""
                )
                db.execute("INSERT OR REPLACE INTO metadata VALUES ('schema_version', '1')")

    # Apps
    def get_app_by_id(self, app_id: str) -> Optional[Dict[str, Any]]:
        with self.connection() as db:
            row = db.execute("SELECT * FROM apps WHERE id = ?", (app_id,)).fetchone()
            return dict(row) if row else None

    def get_app_by_slug(self, slug: str) -> Optional[Dict[str, Any]]:
        with self.connection() as db:
            row = db.execute("SELECT * FROM apps WHERE slug = ?", (slug,)).fetchone()
            return dict(row) if row else None

    def list_apps(self) -> List[Dict[str, Any]]:
        with self.connection() as db:
            rows = db.execute("SELECT * FROM apps ORDER BY id ASC").fetchall()
            return [dict(r) for r in rows]

    def update_app(
        self,
        app_id: str,
        expected_version: int,
        visibility: Optional[str] = None,
        actor: str = "service:awasi-actions",
        now: Optional[datetime] = None,
    ) -> Dict[str, Any]:
        with self.connection() as db:
            cur = db.execute(
                """UPDATE apps SET
                   visibility = CASE WHEN ? IS NOT NULL THEN ? ELSE visibility END,
                   version = version + 1
                   WHERE id = ? AND version = ?""",
                (visibility, visibility, app_id, expected_version),
            )
            if cur.rowcount == 0:
                row = db.execute("SELECT version FROM apps WHERE id = ?", (app_id,)).fetchone()
                if not row:
                    raise NotFoundError(f"App {app_id} not found")
                current_ver = row["version"]
                raise ConflictError(
                    f"Version conflict for app {app_id}: expected {expected_version}, got {current_ver}",
                    current_version=current_ver,
                )
            self.record_event(
                kind="admin.change",
                subject_id=app_id,
                actor=actor,
                destination_host=None,
                record_count=0,
                bytes_count=0,
                fields=["visibility"] if visibility is not None else [],
                outcome="success",
                now=now,
                db=db,
            )
            updated = db.execute("SELECT * FROM apps WHERE id = ?", (app_id,)).fetchone()
            return dict(updated)

    # Agents
    def get_agent(self, agent_id: str) -> Optional[Dict[str, Any]]:
        with self.connection() as db:
            row = db.execute("SELECT * FROM agents WHERE id = ?", (agent_id,)).fetchone()
            return dict(row) if row else None

    def list_agents(self) -> List[Dict[str, Any]]:
        with self.connection() as db:
            rows = db.execute("SELECT * FROM agents ORDER BY id ASC").fetchall()
            return [dict(r) for r in rows]

    def update_agent(
        self,
        agent_id: str,
        expected_version: int,
        status: Optional[str] = None,
        actor: str = "service:awasi-actions",
        now: Optional[datetime] = None,
    ) -> Dict[str, Any]:
        with self.connection() as db:
            cur = db.execute(
                """UPDATE agents SET
                   status = CASE WHEN ? IS NOT NULL THEN ? ELSE status END,
                   version = version + 1
                   WHERE id = ? AND version = ?""",
                (status, status, agent_id, expected_version),
            )
            if cur.rowcount == 0:
                row = db.execute("SELECT version FROM agents WHERE id = ?", (agent_id,)).fetchone()
                if not row:
                    raise NotFoundError(f"Agent {agent_id} not found")
                current_ver = row["version"]
                raise ConflictError(
                    f"Version conflict for agent {agent_id}: expected {expected_version}, got {current_ver}",
                    current_version=current_ver,
                )
            self.record_event(
                kind="admin.change",
                subject_id=agent_id,
                actor=actor,
                destination_host=None,
                record_count=0,
                bytes_count=0,
                fields=["status"] if status is not None else [],
                outcome="success",
                now=now,
                db=db,
            )
            updated = db.execute("SELECT * FROM agents WHERE id = ?", (agent_id,)).fetchone()
            return dict(updated)

    # Destinations
    def get_destination(self, agent_id: str, host: str) -> Optional[Dict[str, Any]]:
        with self.connection() as db:
            row = db.execute("SELECT * FROM destinations WHERE agent_id = ? AND host = ?", (agent_id, host)).fetchone()
            if not row:
                return None
            res = dict(row)
            res["enabled"] = bool(res["enabled"])
            return res

    def list_destinations(self, agent_id: Optional[str] = None) -> List[Dict[str, Any]]:
        with self.connection() as db:
            if agent_id:
                rows = db.execute("SELECT * FROM destinations WHERE agent_id = ? ORDER BY host ASC", (agent_id,)).fetchall()
            else:
                rows = db.execute("SELECT * FROM destinations ORDER BY agent_id ASC, host ASC").fetchall()
            items = []
            for r in rows:
                item = dict(r)
                item["enabled"] = bool(item["enabled"])
                items.append(item)
            return items

    def add_destination(
        self,
        agent_id: str,
        host: str,
        url: str,
        enabled: bool = True,
        version: int = 1,
        actor: str = "service:awasi-actions",
        now: Optional[datetime] = None,
    ) -> Dict[str, Any]:
        with self.connection() as db:
            existing = db.execute(
                "SELECT version FROM destinations WHERE agent_id = ? AND host = ?",
                (agent_id, host),
            ).fetchone()
            if existing:
                raise ConflictError(
                    f"Destination {agent_id}/{host} already exists",
                    current_version=existing["version"],
                )
            agent = db.execute("SELECT id FROM agents WHERE id = ?", (agent_id,)).fetchone()
            if not agent:
                raise NotFoundError(f"Agent {agent_id} not found")
            try:
                db.execute(
                    """INSERT INTO destinations (agent_id, host, url, enabled, version)
                       VALUES (?, ?, ?, ?, ?)""",
                    (agent_id, host, url, 1 if enabled else 0, version),
                )
            except sqlite3.IntegrityError:
                existing = db.execute(
                    "SELECT version FROM destinations WHERE agent_id = ? AND host = ?",
                    (agent_id, host),
                ).fetchone()
                if existing:
                    raise ConflictError(
                        f"Destination {agent_id}/{host} already exists",
                        current_version=existing["version"],
                    )
                raise
            self.record_event(
                kind="admin.change",
                subject_id=agent_id,
                actor=actor,
                destination_host=host,
                record_count=0,
                bytes_count=0,
                fields=["destination", "enabled", "url"],
                outcome="success",
                now=now,
                db=db,
            )
            row = db.execute("SELECT * FROM destinations WHERE agent_id = ? AND host = ?", (agent_id, host)).fetchone()
            res = dict(row)
            res["enabled"] = bool(res["enabled"])
            return res

    def update_destination(
        self,
        agent_id: str,
        host: str,
        expected_version: int,
        enabled: Optional[bool] = None,
        url: Optional[str] = None,
        actor: str = "service:awasi-actions",
        now: Optional[datetime] = None,
    ) -> Dict[str, Any]:
        with self.connection() as db:
            enabled_int = (1 if enabled else 0) if enabled is not None else None
            cur = db.execute(
                """UPDATE destinations SET
                   enabled = CASE WHEN ? IS NOT NULL THEN ? ELSE enabled END,
                   url = CASE WHEN ? IS NOT NULL THEN ? ELSE url END,
                   version = version + 1
                   WHERE agent_id = ? AND host = ? AND version = ?""",
                (enabled_int, enabled_int, url, url, agent_id, host, expected_version),
            )
            if cur.rowcount == 0:
                row = db.execute(
                    "SELECT version FROM destinations WHERE agent_id = ? AND host = ?",
                    (agent_id, host),
                ).fetchone()
                if not row:
                    raise NotFoundError(f"Destination {agent_id}/{host} not found")
                current_ver = row["version"]
                raise ConflictError(
                    f"Version conflict for destination {agent_id}/{host}: expected {expected_version}, got {current_ver}",
                    current_version=current_ver,
                )
            changed_fields = []
            if enabled is not None:
                changed_fields.append("enabled")
            if url is not None:
                changed_fields.append("url")
            self.record_event(
                kind="admin.change",
                subject_id=agent_id,
                actor=actor,
                destination_host=host,
                record_count=0,
                bytes_count=0,
                fields=changed_fields,
                outcome="success",
                now=now,
                db=db,
            )
            updated = db.execute(
                "SELECT * FROM destinations WHERE agent_id = ? AND host = ?",
                (agent_id, host),
            ).fetchone()
            res = dict(updated)
            res["enabled"] = bool(res["enabled"])
            return res

    # Events
    def record_event(
        self,
        kind: str,
        subject_id: str,
        actor: str,
        destination_host: Optional[str] = None,
        record_count: int = 0,
        bytes_count: int = 0,
        fields: Optional[List[str]] = None,
        outcome: str = "success",
        now: Optional[datetime] = None,
        db: Optional[sqlite3.Connection] = None,
    ) -> int:
        occurred = (now or datetime.now(timezone.utc)).isoformat()
        fields_json = json.dumps(fields or [])
        if db is not None:
            cur = db.execute(
                """INSERT INTO events (occurred_at, kind, subject_id, actor, destination_host, record_count, bytes, fields, outcome)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (occurred, kind, subject_id, actor, destination_host, record_count, bytes_count, fields_json, outcome),
            )
            return cur.lastrowid
        with self.connection() as conn:
            cur = conn.execute(
                """INSERT INTO events (occurred_at, kind, subject_id, actor, destination_host, record_count, bytes, fields, outcome)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (occurred, kind, subject_id, actor, destination_host, record_count, bytes_count, fields_json, outcome),
            )
            return cur.lastrowid

    def list_events(self, after: Optional[int] = None, limit: int = 50) -> Dict[str, Any]:
        limit = min(max(1, limit), 100)
        with self.connection() as db:
            if after is not None:
                rows = db.execute(
                    "SELECT * FROM events WHERE id > ? ORDER BY id ASC LIMIT ?",
                    (after, limit + 1),
                ).fetchall()
            else:
                rows = db.execute(
                    "SELECT * FROM events ORDER BY id ASC LIMIT ?",
                    (limit + 1,),
                ).fetchall()

        items = []
        for r in rows[:limit]:
            item = dict(r)
            try:
                item["fields"] = json.loads(item["fields"])
            except Exception:
                item["fields"] = []
            items.append(item)

        has_more = len(rows) > limit
        return {"items": items, "has_more": has_more}

    # Inventory
    def get_inventory(self, company_id: str = "helio", now: Optional[datetime] = None) -> Dict[str, Any]:
        current_time = now or datetime.now(timezone.utc)
        one_hour_ago = (current_time - timedelta(hours=1)).isoformat()

        apps = self.list_apps()
        agents = self.list_agents()

        with self.connection() as db:
            # Enrich apps with anonymous view counts in the last 1 hour
            for app in apps:
                row = db.execute(
                    """SELECT COUNT(*) as cnt, MAX(occurred_at) as last_view
                       FROM events
                       WHERE subject_id = ? AND kind = 'app.view' AND actor = 'anonymous' AND occurred_at >= ?""",
                    (app["id"], one_hour_ago),
                ).fetchone()
                app["anonymous_views_1h"] = row["cnt"] if row else 0
                app["last_anonymous_view_at"] = row["last_view"] if row and row["last_view"] else None

            # Enrich agents with destinations and sent statistics
            for agent in agents:
                dests = self.list_destinations(agent_id=agent["id"])
                for d in dests:
                    h = d["host"]
                    # 1h sent stats
                    row_1h = db.execute(
                        """SELECT SUM(record_count) as total_1h, MAX(occurred_at) as last_sent
                           FROM events
                           WHERE subject_id = ? AND kind = 'agent.send' AND destination_host = ? AND outcome = 'success' AND occurred_at >= ?""",
                        (agent["id"], h, one_hour_ago),
                    ).fetchone()
                    # Total sent stats
                    row_tot = db.execute(
                        """SELECT SUM(record_count) as total_all, MAX(occurred_at) as last_sent
                           FROM events
                           WHERE subject_id = ? AND kind = 'agent.send' AND destination_host = ? AND outcome = 'success'""",
                        (agent["id"], h),
                    ).fetchone()

                    d["records_sent_1h"] = (row_1h["total_1h"] or 0) if row_1h else 0
                    d["records_sent_total"] = (row_tot["total_all"] or 0) if row_tot else 0
                    last_s = (row_tot["last_sent"] or None) if row_tot else None
                    d["last_sent_at"] = last_s

                agent["destinations"] = dests

        return {
            "schema_version": "1",
            "company_id": company_id,
            "complete": True,
            "apps": apps,
            "agents": agents,
        }
