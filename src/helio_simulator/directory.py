"""Persistent directory; seeds once and preserves subsequent company state."""

import sqlite3
from contextlib import contextmanager
from pathlib import Path

from .seed import EMPLOYEES, TEAMS


class Directory:
    def __init__(self, path: Path):
        self.path = path

    @contextmanager
    def connection(self):
        connection = sqlite3.connect(self.path, timeout=10)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    def initialize(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connection() as db:
            # Serialize initial schema/seed creation across simultaneous starts.
            db.execute("BEGIN IMMEDIATE")
            db.execute("""CREATE TABLE IF NOT EXISTS metadata (
                key TEXT PRIMARY KEY, value TEXT NOT NULL
            )""")
            db.execute("""CREATE TABLE IF NOT EXISTS teams (
                id TEXT PRIMARY KEY, name TEXT NOT NULL UNIQUE,
                description TEXT NOT NULL
            )""")
            db.execute("""CREATE TABLE IF NOT EXISTS employees (
                id TEXT PRIMARY KEY, name TEXT NOT NULL,
                email TEXT NOT NULL UNIQUE,
                team_id TEXT NOT NULL REFERENCES teams(id),
                title TEXT NOT NULL,
                status TEXT NOT NULL CHECK(status IN ('active', 'inactive')),
                is_team_lead INTEGER NOT NULL CHECK(is_team_lead IN (0, 1))
            )""")
            db.execute("""CREATE UNIQUE INDEX IF NOT EXISTS one_lead_per_team
                ON employees(team_id) WHERE is_team_lead = 1""")
            version = db.execute(
                "SELECT value FROM metadata WHERE key = 'schema_version'"
            ).fetchone()
            if version and version["value"] != "1":
                raise RuntimeError("Unsupported directory schema version")
            seeded = db.execute(
                "SELECT value FROM metadata WHERE key = 'seed_version'"
            ).fetchone()
            if not seeded:
                db.executemany("INSERT INTO teams VALUES (?, ?, ?)", TEAMS)
                db.executemany(
                    "INSERT INTO employees VALUES (?, ?, ?, ?, ?, 'active', ?)",
                    [
                        (eid, name, eid.removeprefix("emp-").replace("-", ".")
                         + "@helio.example", team, title, lead)
                        for eid, name, team, title, lead in EMPLOYEES
                    ],
                )
                db.execute("INSERT INTO metadata VALUES ('seed_version', '1')")
            db.execute(
                "INSERT OR IGNORE INTO metadata VALUES ('schema_version', '1')"
            )

    def teams(self):
        with self.connection() as db:
            return [dict(row) for row in db.execute("""
                SELECT t.*, COUNT(e.id) AS employee_count,
                    MAX(CASE WHEN e.is_team_lead = 1 THEN e.id END) AS lead_employee_id
                FROM teams t LEFT JOIN employees e ON e.team_id = t.id
                GROUP BY t.id ORDER BY t.name
            """)]

    def employees(self, team_id=None, status=None):
        clauses, values = [], []
        for column, value in (("team_id", team_id), ("status", status)):
            if value is not None:
                clauses.append(f"{column} = ?")
                values.append(value)
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        with self.connection() as db:
            return [dict(row) for row in db.execute(
                "SELECT * FROM employees" + where + " ORDER BY name", values
            )]
