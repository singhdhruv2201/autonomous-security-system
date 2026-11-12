"""Tests for the outside collector service (Phase 4).

TDD: tests written first. Validates POST /collect and GET /received,
per-host aggregation, and confirms no record values are stored.
"""

import pytest
from fastapi.testclient import TestClient

from outside.api import create_app


@pytest.fixture
def outside_client(tmp_path):
    db_path = tmp_path / "outside.sqlite3"
    app = create_app(db_path=db_path)
    with TestClient(app) as client:
        yield client, db_path


def test_outside_collect_and_received_totals(outside_client):
    client, db_path = outside_client

    sample_records = [
        {"id": "cust-northstar", "name": "Northstar Labs", "contact_email": "contact@northstar.example", "plan": "growth"},
        {"id": "cust-orchard", "name": "Orchard Studio", "contact_email": "contact@orchard.example", "plan": "starter"},
    ]

    # POST to /collect with Host: enrich-leads
    resp = client.post("/collect", json=sample_records, headers={"Host": "enrich-leads:8005"})
    assert resp.status_code == 200
    assert resp.json()["status"] == "received"
    assert resp.json()["record_count"] == 2

    # GET /received reports totals for enrich-leads
    resp = client.get("/received")
    assert resp.status_code == 200
    data = resp.json()
    assert data["total_records"] == 2
    assert "enrich-leads" in data["hosts"]
    assert data["hosts"]["enrich-leads"]["records_total"] == 2
    assert data["hosts"]["enrich-leads"]["requests_total"] == 1
    assert "contact_email" in data["hosts"]["enrich-leads"]["fields"]

    # Verify database does not store actual record values
    import sqlite3
    with sqlite3.connect(db_path) as db:
        rows = db.execute("SELECT * FROM collections").fetchall()
        assert len(rows) == 1
        db_content = str(rows)
        assert "Northstar" not in db_content
        assert "northstar.example" not in db_content
        assert "Orchard" not in db_content


def test_outside_collect_multiple_hosts(outside_client):
    client, _ = outside_client

    # 6 records to enrich-leads
    records_6 = [{"id": f"c{i}", "email": f"c{i}@example.com"} for i in range(6)]
    client.post("/collect", json=records_6, headers={"Host": "enrich-leads"})

    # 4 records to partner-api
    records_4 = [{"id": f"c{i}", "email": f"c{i}@example.com"} for i in range(4)]
    client.post("/collect", json=records_4, headers={"Host": "partner-api"})

    resp = client.get("/received")
    data = resp.json()
    assert data["total_records"] == 10
    assert data["hosts"]["enrich-leads"]["records_total"] == 6
    assert data["hosts"]["partner-api"]["records_total"] == 4


def test_outside_cli_execution(outside_client, monkeypatch):
    import io
    import json
    import urllib.response
    from outside.cli import main as cli_main

    client, _ = outside_client

    # Send 6 records to enrich-leads
    records = [{"id": f"c{i}", "email": f"c{i}@example.com"} for i in range(6)]
    client.post("/collect", json=records, headers={"Host": "enrich-leads"})

    def mock_urlopen(req, timeout=5):
        url_str = req.full_url if hasattr(req, "full_url") else str(req)
        resp = client.get("/received")
        fp = io.BytesIO(resp.content)
        res = urllib.response.addinfourl(fp, resp.headers, url_str, resp.status_code)
        res.code = resp.status_code
        return res

    monkeypatch.setattr("urllib.request.urlopen", mock_urlopen)
    cli_main()
