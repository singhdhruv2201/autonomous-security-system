import sqlite3

import pytest
from fastapi.testclient import TestClient

from helio_simulator.api import create_app
from helio_simulator.directory import Directory


def test_company_has_expected_team_sizes_and_valid_leads(client):
    assert client.get("/company").json() == {
        "id": "helio", "name": "Helio", "synthetic": True,
        "team_count": 5, "employee_count": 20,
    }
    teams = client.get("/teams").json()
    assert {t["id"]: t["employee_count"] for t in teams} == {
        "engineering": 8, "sales": 4, "support": 3, "operations": 3, "leadership": 2,
    }
    employees = client.get("/employees").json()
    assert len({e["id"] for e in employees}) == 20
    assert len({e["email"] for e in employees}) == 20
    assert all(e["email"].endswith("@helio.example") for e in employees)
    for team in teams:
        members = client.get(f"/teams/{team['id']}").json()["employees"]
        assert len(members) == team["employee_count"]
        leads = [e for e in members if e["is_team_lead"]]
        assert len(leads) == 1
        assert leads[0]["id"] == team["lead_employee_id"]
        assert all(e["team_id"] == team["id"] for e in members)


def test_employee_queries_and_validation(client):
    sales = client.get("/employees", params={"team_id": "sales", "status": "active"}).json()
    assert len(sales) == 4
    assert all(e["team_id"] == "sales" and e["status"] == "active" for e in sales)
    maya = client.get("/employees/emp-maya-chen").json()
    assert maya["name"] == "Maya Chen"
    assert maya["team_id"] == "sales"
    assert client.get("/employees?status=inactive").json() == []
    assert client.get("/employees?status=invalid").status_code == 422
    assert client.get("/employees?team_id=unknown").status_code == 404
    assert client.get("/teams/unknown").status_code == 404
    assert client.get("/employees/unknown").status_code == 404


def test_restart_preserves_company_state_and_ids(tmp_path):
    path = tmp_path / "company.sqlite3"
    with TestClient(create_app(path)) as first:
        original_ids = [e["id"] for e in first.get("/employees").json()]
    with Directory(path).connection() as db:
        db.execute("UPDATE employees SET status = 'inactive' WHERE id = 'emp-maya-chen'")
    with TestClient(create_app(path)) as second:
        assert [e["id"] for e in second.get("/employees").json()] == original_ids
        assert second.get("/employees/emp-maya-chen").json()["status"] == "inactive"
        assert second.get("/company").json()["employee_count"] == 20


def test_database_rejects_invalid_memberships_and_duplicate_leads(tmp_path):
    directory = Directory(tmp_path / "company.sqlite3")
    directory.initialize()
    with pytest.raises(sqlite3.IntegrityError), directory.connection() as db:
        db.execute("UPDATE employees SET team_id = 'missing' WHERE id = 'emp-maya-chen'")
    with pytest.raises(sqlite3.IntegrityError), directory.connection() as db:
        db.execute("UPDATE employees SET is_team_lead = 1 WHERE id = 'emp-maya-chen'")


def test_directory_is_read_only_and_healthy(client):
    assert client.get("/health").json() == {"status": "ok"}
    assert client.get("/openapi.json").status_code == 200
    assert client.post("/employees", json={"name": "Injected"}).status_code == 405
    assert client.patch("/employees/emp-maya-chen", json={"status": "inactive"}).status_code == 405
