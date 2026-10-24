"""Upgrade the original on-disk schema without losing company state."""

import sqlite3

import pytest
from fastapi.testclient import TestClient

from helio_simulator.api import create_app
from helio_simulator.directory import Directory
from helio_simulator.seed import EMPLOYEES, TEAMS


@pytest.fixture
def legacy_directory(tmp_path):
    directory = Directory(tmp_path / "legacy.sqlite3")
    # Keep the original DDL here: using the current initializer would not test
    # whether an actual version-1 database can be upgraded.
    with directory.connection() as db:
        db.executescript("""
            CREATE TABLE metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE teams (
                id TEXT PRIMARY KEY, name TEXT NOT NULL UNIQUE,
                description TEXT NOT NULL
            );
            CREATE TABLE employees (
                id TEXT PRIMARY KEY, name TEXT NOT NULL,
                email TEXT NOT NULL UNIQUE,
                team_id TEXT NOT NULL REFERENCES teams(id),
                title TEXT NOT NULL,
                status TEXT NOT NULL CHECK(status IN ('active', 'inactive')),
                is_team_lead INTEGER NOT NULL CHECK(is_team_lead IN (0, 1))
            );
            CREATE UNIQUE INDEX one_lead_per_team
                ON employees(team_id) WHERE is_team_lead = 1;
            INSERT INTO metadata VALUES ('schema_version', '1'), ('seed_version', '1');
        """)
        db.executemany("INSERT INTO teams VALUES (?, ?, ?)", TEAMS)
        db.executemany("INSERT INTO employees VALUES (?, ?, ?, ?, ?, 'active', ?)", [
            (eid, name, eid.removeprefix("emp-").replace("-", ".") + "@helio.example",
             team, title, lead)
            for eid, name, team, title, lead in EMPLOYEES
        ])
    return directory


def snapshot(directory):
    with directory.connection() as db:
        return list(db.iterdump())


def test_startup_migrates_legacy_database_and_preserves_company_state(legacy_directory):
    with legacy_directory.connection() as db:
        db.execute("UPDATE employees SET status = 'inactive' WHERE id = 'emp-maya-chen'")
        db.execute("DELETE FROM employees WHERE id = 'emp-luca-rossi'")
        db.execute("INSERT INTO teams VALUES ('research', 'Research', 'New team')")
    expected = (legacy_directory.teams(), legacy_directory.employees())
    for _ in range(2):
        with TestClient(create_app(legacy_directory.path)) as client:
            assert client.get("/health").status_code == 200
            assert client.get("/teams").status_code == 200
            assert client.get("/employees").status_code == 200
            assert client.get("/company").json()["employee_count"] == 19
            assert client.get("/employees/emp-maya-chen").json()["status"] == "inactive"
        assert (legacy_directory.teams(), legacy_directory.employees()) == expected
    with legacy_directory.connection() as db:
        assert dict(db.execute("SELECT key, value FROM metadata")) == {
            "schema_version": "2", "seed_version": "1",
        }
        assert db.execute("PRAGMA foreign_key_check").fetchall() == []
        assert db.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        for table in ("teams", "employees"):
            columns = db.execute(f"PRAGMA table_info({table})").fetchall()
            assert next(column for column in columns if column["name"] == "id")["notnull"] == 1
        assert db.execute("PRAGMA foreign_key_list(employees)").fetchone()["table"] == "teams"


@pytest.mark.parametrize("statement", [
    "INSERT INTO teams VALUES (NULL, 'Invalid team', 'Missing ID')",
    """INSERT INTO employees VALUES
        (NULL, 'Invalid', 'invalid@helio.example', 'sales', 'Test', 'active', 0)""",
    "UPDATE teams SET id = NULL WHERE id = 'empty'",
    "UPDATE employees SET id = NULL WHERE id = 'emp-maya-chen'",
    "UPDATE employees SET team_id = 'missing' WHERE id = 'emp-maya-chen'",
    "UPDATE employees SET is_team_lead = 1 WHERE id = 'emp-maya-chen'",
    "DELETE FROM teams WHERE id = 'sales'",
])
def test_migration_enforces_new_and_existing_constraints(legacy_directory, statement):
    legacy_directory.initialize()
    with legacy_directory.connection() as db:
        db.execute("INSERT INTO teams VALUES ('empty', 'Empty', 'No members')")
    before = snapshot(legacy_directory)
    with pytest.raises(sqlite3.IntegrityError):
        with legacy_directory.connection() as db:
            db.execute(statement)
    assert snapshot(legacy_directory) == before


@pytest.mark.parametrize("table,statement", [
    ("teams", "INSERT INTO teams VALUES (NULL, 'Invalid team', 'Missing ID')"),
    ("employees", """INSERT INTO employees VALUES
        (NULL, 'Invalid', 'invalid@helio.example', 'sales', 'Test', 'active', 0)"""),
])
def test_migration_refuses_null_ids_without_altering_legacy_data(legacy_directory, table, statement):
    with legacy_directory.connection() as db:
        db.execute(statement)
    before = snapshot(legacy_directory)
    with pytest.raises(RuntimeError, match=f"{table} contains NULL IDs"):
        legacy_directory.initialize()
    assert snapshot(legacy_directory) == before


def test_failed_copy_rolls_back_migration_and_allows_retry(legacy_directory):
    # Simulate an old external writer that did not enforce foreign keys.
    with sqlite3.connect(legacy_directory.path) as db:
        db.execute("UPDATE employees SET team_id = 'missing' WHERE id = 'emp-maya-chen'")
    before = snapshot(legacy_directory)
    with pytest.raises(sqlite3.IntegrityError):
        legacy_directory.initialize()
    assert snapshot(legacy_directory) == before
    with legacy_directory.connection() as db:
        db.execute("UPDATE employees SET team_id = 'sales' WHERE id = 'emp-maya-chen'")
    legacy_directory.initialize()
    assert len(legacy_directory.employees()) == 20
    with legacy_directory.connection() as db:
        assert db.execute("PRAGMA foreign_key_check").fetchall() == []
