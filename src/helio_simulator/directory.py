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
            version = db.execute(
                "SELECT value FROM metadata WHERE key = 'schema_version'"
            ).fetchone()
            if version and version["value"] not in ("1", "2"):
                raise RuntimeError("Unsupported directory schema version")
            if version and version["value"] == "1":
                self._migrate_non_null_ids(db)
            else:
                self._create_tables(db)
            db.execute("""CREATE UNIQUE INDEX IF NOT EXISTS one_lead_per_team
                ON employees(team_id) WHERE is_team_lead = 1""")
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
                "INSERT OR REPLACE INTO metadata VALUES ('schema_version', '2')"
            )

    @staticmethod
    def _create_tables(db, suffix=""):
        # The suffix is an internal migration constant, never external input.
        db.execute(f"""CREATE TABLE IF NOT EXISTS teams{suffix} (
            id TEXT PRIMARY KEY NOT NULL, name TEXT NOT NULL UNIQUE,
            description TEXT NOT NULL
        )""")
        db.execute(f"""CREATE TABLE IF NOT EXISTS employees{suffix} (
            id TEXT PRIMARY KEY NOT NULL, name TEXT NOT NULL,
            email TEXT NOT NULL UNIQUE,
            team_id TEXT NOT NULL REFERENCES teams{suffix}(id),
            title TEXT NOT NULL,
            status TEXT NOT NULL CHECK(status IN ('active', 'inactive')),
            is_team_lead INTEGER NOT NULL CHECK(is_team_lead IN (0, 1))
        )""")

    def _migrate_non_null_ids(self, db):
        # Refuse invalid legacy data instead of deleting records or inventing IDs.
        for table in ("teams", "employees"):
            if db.execute(f"SELECT 1 FROM {table} WHERE id IS NULL LIMIT 1").fetchone():
                raise RuntimeError(
                    f"Cannot migrate directory: {table} contains NULL IDs; "
                    "repair those records before restarting"
                )
        # Rebuild both related tables inside initialize's transaction. Copy the
        # parent first and drop the child first to keep foreign keys enabled.
        self._create_tables(db, suffix="_v2")
        db.execute("INSERT INTO teams_v2 SELECT * FROM teams")
        db.execute("INSERT INTO employees_v2 SELECT * FROM employees")
        db.execute("DROP TABLE employees")
        db.execute("DROP TABLE teams")
        db.execute("ALTER TABLE teams_v2 RENAME TO teams")
        db.execute("ALTER TABLE employees_v2 RENAME TO employees")

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
