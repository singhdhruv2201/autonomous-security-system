"""Opt-in operation evidence at existing HTTP and CLI boundaries; with correlated HTTP lifecycle evidence."""

import json
import secrets
import sys
import urllib.error
import urllib.request

import pytest
from fastapi.testclient import TestClient

from helio_crm.api import create_app
from helio_crm.store import CRMStore
from test_crm_activity import events, pending
from test_graph import serve


def operations(store):
    return [e for e in events(store) if e["event_type"] == "operation.completed"]


@pytest.fixture
def setup(directory, tmp_path):
    tokens = {key: secrets.token_urlsafe(32) for key in ("admin", "maya", "agent", "observer")}
    return directory, tmp_path / "crm.sqlite3", tokens


def test_factory_enables_capture_without_new_routes(setup):
    directory, path, tokens = setup
    with TestClient(create_app(path, tokens, directory.employees, activity_enabled=True)) as client:
        headers = {"Authorization": "Bearer " + tokens["agent"]}
        assert len(client.get("/customers", headers=headers).json()) == 6
        assert client.get("/customers").status_code == 401
        assert client.patch("/customers/cust-northstar", headers=headers,
                            json={"expected_version": 1, "notes": "denied"}).status_code == 403
        assert client.get("/security-events", headers=headers).status_code == 404
    store = CRMStore(path)
    assert [e["status_code"] for e in operations(store)] == [200, 401, 403]
    assert all(e["event_type"] == "operation.completed" for e in operations(store))
    assert len(pending(store)) == 15


@pytest.mark.parametrize("value, expected", [(None, False), ("0", False), ("1", True)])
def test_environment_capture_is_explicit_opt_in(setup, monkeypatch, value, expected):
    directory, path, tokens = setup
    monkeypatch.delenv("CRM_ACTIVITY_ENABLED", raising=False)
    if value is not None:
        monkeypatch.setenv("CRM_ACTIVITY_ENABLED", value)
    with TestClient(create_app(path, tokens, directory.employees)) as client:
        assert client.get("/customers", headers={"Authorization": "Bearer " + tokens["agent"]}).status_code == 200
    assert len(events(CRMStore(path))) == 4 * int(expected)


@pytest.mark.parametrize("value", ["false", "true", "", "2"])
def test_bad_environment_flag_is_rejected(setup, monkeypatch, value):
    directory, path, tokens = setup
    monkeypatch.setenv("CRM_ACTIVITY_ENABLED", value)
    with pytest.raises(ValueError, match="CRM_ACTIVITY_ENABLED"):
        create_app(path, tokens, directory.employees)
    assert not path.exists()


def test_explicit_factory_flag_overrides_environment_and_disabled_retains_audit(setup, monkeypatch):
    directory, path, tokens = setup
    monkeypatch.setenv("CRM_ACTIVITY_ENABLED", "1")
    with TestClient(create_app(path, tokens, directory.employees, activity_enabled=False)) as client:
        assert client.get("/customers", headers={"Authorization": "Bearer " + tokens["agent"]}).status_code == 200
    store = CRMStore(path)
    assert events(store) == []
    assert len(store.audit(tokens["admin"])) == 1


def test_http_storage_failure_has_safe_503_and_rolls_back(setup):
    directory, path, tokens = setup
    store = CRMStore(path)
    with TestClient(create_app(path, tokens, directory.employees, activity_enabled=True)) as client:
        with store.connection() as db:
            db.execute("""CREATE TRIGGER reject_outbox BEFORE INSERT ON activity_outbox
                BEGIN SELECT RAISE(ABORT, 'secret database detail'); END""")
        response = client.patch("/customers/cust-northstar",
                                headers={"Authorization": "Bearer " + tokens["maya"]},
                                json={"expected_version": 1, "notes": "never committed"})
        assert response.status_code == 503
        assert response.json() == {"detail": "CRM storage is temporarily unavailable"}
    assert events(store) == [] and pending(store) == []
    with store.connection() as db:
        assert db.execute("SELECT notes FROM customers WHERE id='cust-northstar'").fetchone()[0] == ""


def test_unexpected_application_error_has_safe_http_500_and_failure_event(setup, monkeypatch):
    directory, path, tokens = setup

    def broken(db, customer_id):
        db.execute("UPDATE customers SET notes='partial' WHERE id=?", (customer_id,))
        raise RuntimeError("credential=" + tokens["admin"])

    with TestClient(create_app(path, tokens, directory.employees, activity_enabled=True)) as client:
        monkeypatch.setattr(CRMStore, "_customer", staticmethod(broken))
        response = client.get("/customers/cust-northstar", headers={"Authorization": "Bearer " + tokens["agent"]})
        assert response.status_code == 500 and response.json() == {"detail": "Operation failed"}
    store = CRMStore(path)
    [event] = operations(store)
    assert event["reason_code"] == "operation_failed" and event["outcome"] == "failed"
    assert tokens["admin"] not in json.dumps(event)
    with store.connection() as db:
        assert db.execute("SELECT notes FROM customers WHERE id='cust-northstar'").fetchone()[0] == ""


def test_framework_rejections_and_public_routes_record_only_http_stages(setup):
    directory, path, tokens = setup
    with TestClient(create_app(path, tokens, directory.employees, activity_enabled=True)) as client:
        for route in ("/", "/health", "/docs", "/openapi.json"):
            assert client.get(route).status_code == 200
        assert client.get("/missing").status_code == 404
        assert client.delete("/customers").status_code == 405
        assert client.patch("/customers/cust-northstar",
                            headers={"Authorization": "Bearer " + tokens["maya"]},
                            json={"notes": "invalid without version"}).status_code == 422
    store = CRMStore(path)
    assert len(events(store)) == len(pending(store)) == 9
    assert len(events(store, "operational_events")) == 15
    assert operations(store) == []


def test_real_http_cli_and_agent_operations_are_durable(setup, monkeypatch, capsys):
    from helio_crm import control
    from helio_crm.agent import run
    directory, path, tokens = setup
    with serve(create_app(path, tokens, directory.employees, activity_enabled=True)) as url:
        assert run(url, tokens["agent"])["customer_count"] == 6
        monkeypatch.setenv("CRM_URL", url)
        monkeypatch.setenv("CRM_ADMIN_TOKEN", tokens["admin"])
        monkeypatch.setenv("CRM_MAYA_TOKEN", tokens["maya"])
        monkeypatch.setattr(sys, "argv", ["crm-control", "update-customer"])
        control.main()
        assert json.loads(capsys.readouterr().out)["version"] == 2
        req = urllib.request.Request(url + "/grants/grant-sales-agent/revoke", method="POST",
                                     data=b'{"expected_version":1}',
                                     headers={"Authorization": "Bearer " + tokens["admin"],
                                              "Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=5) as response:
            assert json.load(response)["status"] == "revoked"
        with pytest.raises(urllib.error.HTTPError) as caught:
            run(url, tokens["agent"])
        assert caught.value.code == 403
    store = CRMStore(path)
    recorded = operations(store)
    assert [e["action"] for e in recorded] == ["customers:read", "customers:read", "customers:write",
                                               "grants:revoke", "customers:read"]
    assert recorded[-1]["outcome"] == "denied"
    assert len(pending(store)) == 20
    with serve(create_app(path, tokens, lambda: pytest.fail("Must not rebootstrap"), activity_enabled=True)) as url:
        with pytest.raises(urllib.error.HTTPError):
            run(url, tokens["agent"])
    assert operations(store)[:5] == recorded and len(pending(store)) == 24
