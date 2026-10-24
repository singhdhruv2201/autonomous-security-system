"""Exercise storage directly, without an HTTP server or a persistent user DB."""

import sqlite3
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

import pytest

import helio_simulator.directory as directory_module
from helio_simulator.directory import Directory


def test_initialize_creates_parent_directories_and_stable_fixtures(tmp_path):
    first = Directory(tmp_path / "nested" / "first.sqlite3")
    second = Directory(tmp_path / "other" / "second.sqlite3")
    first.initialize()
    second.initialize()
    assert first.path.is_file()
    assert first.teams() == second.teams()
    assert first.employees() == second.employees()
    assert len(first.teams()) == 5
    assert len(first.employees()) == 20
    assert all(person["status"] == "active" for person in first.employees())
    with first.connection() as db:
        assert dict(db.execute("SELECT key, value FROM metadata")) == {
            "schema_version": "2", "seed_version": "1",
        }


def test_reinitialization_preserves_edits_deletions_and_additions(directory):
    with directory.connection() as db:
        db.execute("UPDATE employees SET status = 'inactive' WHERE id = 'emp-maya-chen'")
        db.execute("DELETE FROM employees WHERE id = 'emp-luca-rossi'")
        db.execute("INSERT INTO teams VALUES ('research', 'Research', 'New team')")
    expected = (directory.teams(), directory.employees())
    for _ in range(3):
        Directory(directory.path).initialize()
        assert (directory.teams(), directory.employees()) == expected


def test_simultaneous_first_start_seeds_exactly_once(tmp_path):
    path = tmp_path / "shared.sqlite3"
    barrier = Barrier(6)

    def initialize():
        barrier.wait(timeout=10)
        directory = Directory(path)
        directory.initialize()
        return directory.teams(), directory.employees()

    with ThreadPoolExecutor(max_workers=6) as pool:
        results = list(pool.map(lambda _: initialize(), range(6)))
    assert all(result == results[0] for result in results)
    assert len(results[0][0]) == 5
    assert len(results[0][1]) == 20
    with Directory(path).connection() as db:
        assert db.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert db.execute("PRAGMA foreign_key_check").fetchall() == []


def test_failed_seed_rolls_back_and_can_be_retried(tmp_path, monkeypatch):
    directory = Directory(tmp_path / "company.sqlite3")
    # Fail after valid rows have already been inserted.
    with monkeypatch.context() as patch:
        patch.setattr(directory_module, "EMPLOYEES", directory_module.EMPLOYEES + (
            ("emp-invalid", "Invalid", "missing-team", "Engineer", False),
        ))
        with pytest.raises(sqlite3.IntegrityError):
            directory.initialize()
    with directory.connection() as db:
        assert db.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'"
        ).fetchall() == []
    directory.initialize()
    assert len(directory.teams()) == 5
    assert len(directory.employees()) == 20


def test_unsupported_schema_version_fails_without_changing_data(directory):
    with directory.connection() as db:
        db.execute("UPDATE metadata SET value = '999' WHERE key = 'schema_version'")
    before = (directory.teams(), directory.employees())
    with pytest.raises(RuntimeError, match="Unsupported directory schema version"):
        directory.initialize()
    assert (directory.teams(), directory.employees()) == before
    with directory.connection() as db:
        assert db.execute(
            "SELECT value FROM metadata WHERE key = 'schema_version'"
        ).fetchone()[0] == "999"


def test_connection_commits_closes_and_enables_foreign_keys(directory):
    with directory.connection() as db:
        assert db.execute("PRAGMA foreign_keys").fetchone()[0] == 1
        db.execute("UPDATE employees SET status = 'inactive' WHERE id = 'emp-maya-chen'")
    with pytest.raises(sqlite3.ProgrammingError, match="closed"):
        db.execute("SELECT 1")
    assert [e["id"] for e in directory.employees(status="inactive")] == ["emp-maya-chen"]


def test_connection_rolls_back_all_writes_on_exception(directory):
    before = directory.employees()
    with pytest.raises(RuntimeError, match="abort"):
        with directory.connection() as db:
            db.execute("DELETE FROM employees WHERE id = 'emp-maya-chen'")
            raise RuntimeError("abort")
    assert directory.employees() == before
    with pytest.raises(sqlite3.ProgrammingError, match="closed"):
        db.execute("SELECT 1")


@pytest.mark.parametrize("column,value", [
    ("id", "emp-hannah-reed"),
    ("email", "hannah.reed@helio.example"),
    ("team_id", "missing-team"),
    ("status", "terminated"),
    ("status", ""),
    ("is_team_lead", -1),
    ("is_team_lead", 2),
    ("is_team_lead", 1),
    *[(column, None) for column in (
        "name", "email", "team_id", "title", "status", "is_team_lead",
    )],
])
def test_invalid_employee_updates_are_rejected_atomically(directory, column, value):
    before = directory.employees()
    with pytest.raises(sqlite3.IntegrityError):
        with directory.connection() as db:
            db.execute("UPDATE employees SET title = 'Changed' WHERE id = 'emp-theo-martin'")
            # Column names come only from the fixed test cases above.
            db.execute(f"UPDATE employees SET {column} = ? WHERE id = 'emp-maya-chen'", (value,))
    assert directory.employees() == before


@pytest.mark.parametrize("column,value", [
    ("id", "engineering"), ("name", "Engineering"),
    ("name", None), ("description", None),
])
def test_invalid_team_updates_are_rejected(directory, column, value):
    before = directory.teams()
    with pytest.raises(sqlite3.IntegrityError):
        with directory.connection() as db:
            db.execute(f"UPDATE teams SET {column} = ? WHERE id = 'sales'", (value,))
    assert directory.teams() == before


def test_team_with_members_cannot_be_deleted(directory):
    with pytest.raises(sqlite3.IntegrityError):
        with directory.connection() as db:
            db.execute("DELETE FROM teams WHERE id = 'sales'")
    assert len(directory.employees(team_id="sales")) == 4


def test_empty_team_and_team_without_lead_are_preserved(directory):
    with directory.connection() as db:
        db.execute("INSERT INTO teams VALUES ('empty', 'Empty', 'No members')")
        db.execute("UPDATE employees SET is_team_lead = 0 WHERE team_id = 'sales'")
    teams = {team["id"]: team for team in directory.teams()}
    assert teams["empty"]["employee_count"] == 0
    assert teams["empty"]["lead_employee_id"] is None
    assert teams["sales"]["employee_count"] == 4
    assert teams["sales"]["lead_employee_id"] is None


@pytest.mark.parametrize("filters", [
    {}, {"team_id": "sales"}, {"status": "active"}, {"status": "inactive"},
    {"team_id": "sales", "status": "active"},
    {"team_id": "sales", "status": "inactive"},
    {"team_id": "engineering", "status": "inactive"},
    {"team_id": "missing"}, {"team_id": ""}, {"status": ""},
    {"team_id": "sales' OR 1=1 --"}, {"status": "active' OR 1=1 --"},
])
def test_filters_match_all_conditions_and_sort_by_name(directory, filters):
    with directory.connection() as db:
        db.execute("UPDATE employees SET status = 'inactive' WHERE id = 'emp-maya-chen'")
    expected = sorted(
        (person for person in directory.employees()
         if all(person[key] == value for key, value in filters.items())),
        key=lambda person: person["name"],
    )
    assert directory.employees(**filters) == expected


def test_teams_are_sorted_by_name_and_counts_include_inactive_members(directory):
    with directory.connection() as db:
        db.execute("UPDATE employees SET status = 'inactive' WHERE team_id = 'sales'")
    teams = directory.teams()
    assert [team["name"] for team in teams] == sorted(team["name"] for team in teams)
    sales = next(team for team in teams if team["id"] == "sales")
    assert sales["employee_count"] == 4
    assert sales["lead_employee_id"] == "emp-hannah-reed"


@pytest.mark.parametrize("statement", [
    pytest.param(
        "INSERT INTO teams VALUES (NULL, 'Invalid team', 'Missing ID')",
        id="team-id",
    ),
    pytest.param(
        """INSERT INTO employees VALUES
        (NULL, 'Invalid employee', 'invalid@helio.example', 'sales', 'Test', 'active', 0)""",
        id="employee-id",
    ),
    pytest.param("UPDATE teams SET id = NULL WHERE id = 'empty'", id="update-team-id"),
    pytest.param(
        "UPDATE employees SET id = NULL WHERE id = 'emp-maya-chen'",
        id="update-employee-id",
    ),
])
def test_primary_keys_cannot_be_null(directory, statement):
    # NULL IDs cannot satisfy the API's required string IDs. SQLite's TEXT
    # PRIMARY KEY alone does not enforce NOT NULL on these rowid tables.
    with directory.connection() as db:
        db.execute("INSERT INTO teams VALUES ('empty', 'Empty', 'No members')")
    with pytest.raises(sqlite3.IntegrityError):
        with directory.connection() as db:
            db.execute(statement)
