"""API contract and failure-path tests against isolated SQLite databases."""

import sqlite3

import pytest
from fastapi.testclient import TestClient

from helio_simulator.api import create_app
from helio_simulator.directory import Directory


def test_root_redirects_to_working_docs(client):
    response = client.get("/", follow_redirects=False)
    assert response.status_code == 307
    assert response.headers["location"] == "/docs"
    assert client.get("/docs").status_code == 200


def test_openapi_exposes_only_read_endpoints(client):
    paths = client.get("/openapi.json").json()["paths"]
    assert set(paths) == {
        "/company", "/teams", "/teams/{team_id}",
        "/employees", "/employees/{employee_id}", "/health",
    }
    assert all(set(operations) == {"get"} for operations in paths.values())


def test_employee_list_and_detail_have_consistent_typed_fields(client):
    response = client.get("/employees")
    assert response.status_code == 200
    employees = response.json()
    assert [person["name"] for person in employees] == sorted(p["name"] for p in employees)
    for person in employees:
        assert set(person) == {
            "id", "name", "email", "team_id", "title", "status", "is_team_lead",
        }
        assert type(person["is_team_lead"]) is bool
        assert all(isinstance(value, str) for key, value in person.items() if key != "is_team_lead")
        detail = client.get(f"/employees/{person['id']}")
        assert detail.status_code == 200
        assert detail.json() == person


@pytest.mark.parametrize("status", ["", "ACTIVE", "terminated", "null", "active' OR 1=1 --"])
def test_invalid_status_filters_return_validation_error(client, status):
    response = client.get("/employees", params={"status": status})
    assert response.status_code == 422
    assert any(error["loc"] == ["query", "status"] for error in response.json()["detail"])


@pytest.mark.parametrize("team_id", ["", "missing", "Sales", "sales' OR 1=1 --"])
def test_unknown_team_filter_returns_404(client, team_id):
    response = client.get("/employees", params={"team_id": team_id})
    assert response.status_code == 404
    assert response.json() == {"detail": "Team not found"}


@pytest.mark.parametrize("path,detail", [
    ("/teams/missing", "Team not found"),
    ("/employees/missing", "Employee not found"),
])
def test_unknown_resource_errors(client, path, detail):
    response = client.get(path)
    assert response.status_code == 404
    assert response.json() == {"detail": detail}


@pytest.mark.parametrize("method", ["POST", "PUT", "PATCH", "DELETE"])
@pytest.mark.parametrize("path", [
    "/company", "/teams", "/teams/sales", "/employees",
    "/employees/emp-maya-chen", "/health",
])
def test_all_directory_routes_reject_writes_without_changing_state(client, directory, method, path):
    before = (directory.teams(), directory.employees())
    response = client.request(method, path, json={"status": "inactive", "name": "Injected"})
    assert response.status_code == 405
    assert (directory.teams(), directory.employees()) == before


def test_filters_and_counts_reflect_changes_after_startup(client, directory):
    with directory.connection() as db:
        db.execute("UPDATE employees SET status = 'inactive' WHERE id = 'emp-maya-chen'")
    inactive = client.get("/employees", params={"team_id": "sales", "status": "inactive"})
    assert inactive.status_code == 200
    assert [person["id"] for person in inactive.json()] == ["emp-maya-chen"]
    active = client.get("/employees", params={"team_id": "sales", "status": "active"}).json()
    assert len(active) == 3
    assert all(person["id"] != "emp-maya-chen" for person in active)
    assert client.get("/teams/sales").json()["employee_count"] == 4
    assert client.get("/company").json()["employee_count"] == 20
    with directory.connection() as db:
        db.execute("DELETE FROM employees WHERE id = 'emp-maya-chen'")
    assert client.get("/employees/emp-maya-chen").status_code == 404
    assert client.get("/teams/sales").json()["employee_count"] == 3
    assert client.get("/company").json()["employee_count"] == 19


def test_empty_team_is_a_valid_resource(client, directory):
    with directory.connection() as db:
        db.execute("INSERT INTO teams VALUES ('empty', 'Empty', 'No members yet')")
    response = client.get("/teams/empty")
    assert response.status_code == 200
    assert response.json() == {
        "id": "empty", "name": "Empty", "description": "No members yet",
        "employee_count": 0, "lead_employee_id": None, "employees": [],
    }
    assert client.get("/employees", params={"team_id": "empty"}).json() == []
    assert client.get("/company").json()["team_count"] == 6


@pytest.mark.parametrize("explicit_path", [False, True])
def test_database_path_configuration(tmp_path, monkeypatch, explicit_path):
    environment_path = tmp_path / "environment.sqlite3"
    override_path = tmp_path / "explicit.sqlite3"
    monkeypatch.setenv("HELIO_DB_PATH", str(environment_path))
    app = create_app(override_path) if explicit_path else create_app()
    selected = override_path if explicit_path else environment_path
    assert not selected.exists()  # Initialization belongs to startup.
    with TestClient(app) as client:
        assert client.get("/health").json() == {"status": "ok"}
        assert selected.is_file()
    assert not (environment_path if explicit_path else override_path).exists()


def test_default_database_path_is_relative_to_working_directory(tmp_path, monkeypatch):
    monkeypatch.delenv("HELIO_DB_PATH", raising=False)
    monkeypatch.chdir(tmp_path)
    with TestClient(create_app()) as client:
        assert client.get("/company").json()["employee_count"] == 20
    assert (tmp_path / "data" / "helio.sqlite3").is_file()


def test_apps_with_distinct_paths_do_not_share_state(tmp_path):
    first_path = tmp_path / "first.sqlite3"
    with TestClient(create_app(first_path)) as first, TestClient(
        create_app(tmp_path / "second.sqlite3")
    ) as second:
        with Directory(first_path).connection() as db:
            db.execute("DELETE FROM employees WHERE id = 'emp-maya-chen'")
        assert first.get("/employees/emp-maya-chen").status_code == 404
        assert second.get("/employees/emp-maya-chen").status_code == 200
        assert second.get("/company").json()["employee_count"] == 20


def test_health_does_not_report_success_when_database_is_unreadable(directory):
    with TestClient(create_app(directory.path), raise_server_exceptions=False) as client:
        directory.path.write_bytes(b"This is not a SQLite database.")
        assert client.get("/health").status_code == 500


def test_startup_rejects_corrupt_database_without_overwriting_it(tmp_path):
    path = tmp_path / "corrupt.sqlite3"
    original = b"This is not a SQLite database."
    path.write_bytes(original)
    with pytest.raises(sqlite3.DatabaseError):
        with TestClient(create_app(path)):
            pytest.fail("Startup should reject a corrupt database")
    assert path.read_bytes() == original


def test_startup_rejects_unsupported_schema(directory):
    with directory.connection() as db:
        db.execute("UPDATE metadata SET value = '999' WHERE key = 'schema_version'")
    with pytest.raises(RuntimeError, match="Unsupported directory schema version"):
        with TestClient(create_app(directory.path)):
            pytest.fail("Startup should reject an unsupported schema")
