import copy
import json
import secrets
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Thread
from socketserver import TCPServer

import pytest

from helio_crm.store import CRMStore
from helio_security.connectors import ConnectorError, fetch_snapshot
from helio_security.models import normalize
from helio_security.setup import setup


@pytest.fixture
def inventories(directory, tmp_path):
    tokens = {key: secrets.token_urlsafe(32) for key in ("admin", "maya", "agent", "observer")}
    store = CRMStore(tmp_path / "crm.sqlite3")
    store.initialize(directory.employees(), tokens)
    return directory.snapshot(), store.inventory(tokens["observer"])


def test_normalization_models_real_identity_access_and_metadata_only(inventories):
    directory, crm = inventories
    crm["customers"] = [{"notes": "secret customer content"}]
    crm["accounts"][0]["token_hash"] = "secret credential"
    d = normalize("directory", directory)
    c = normalize("crm", crm)
    assert len(d.nodes) == 25 and len(d.edges) == 20
    assert sum(n["kind"] == "Agent" for n in c.nodes) == 1
    assert sum(n["kind"] == "AppAccount" for n in c.nodes) == 20
    assert sum(n["kind"] == "AccessGrant" for n in c.nodes) == 3
    assert any(e["kind"] == "OWNS" and e["start"] == ["Employee", "emp-maya-chen"]
               and e["end"] == ["Agent", "agent:sales-research"] for e in c.edges)
    assert any(e["kind"] == "ON" and e["end"] == ["DataResource", "customer-database"] for e in c.edges)
    assert "secret" not in json.dumps(c.nodes)


@pytest.mark.parametrize("change", [
    lambda p: p.update(complete=False),
    lambda p: p.update(schema_version=2),
    lambda p: p.update(company_id="other-company"),
    lambda p: p["employees"].append(p["employees"][0]),
    lambda p: p["employees"][0].update(team_id="missing"),
    lambda p: p["employees"][0].update(status="unknown"),
    lambda p: p.pop("teams"),
])
def test_incomplete_or_invalid_directory_is_rejected(inventories, change):
    payload = copy.deepcopy(inventories[0])
    change(payload)
    with pytest.raises(ValueError):
        normalize("directory", payload)


@pytest.mark.parametrize("change", [
    lambda p: p["grants"][0].update(principal_id="missing"),
    lambda p: p["grants"][0].update(resource_id="missing"),
    lambda p: p["grants"][0].update(app_id="other-app"),
    lambda p: p["grants"][0].update(owner_employee_id="other-owner"),
    lambda p: p["accounts"].append(p["accounts"][0]),
    lambda p: p["grants"][0].update(version=0),
])
def test_invalid_crm_references_are_rejected(inventories, change):
    payload = copy.deepcopy(inventories[1])
    change(payload)
    with pytest.raises(ValueError):
        normalize("crm", payload)


@contextmanager
def http_source(payload, status=200, location=None):
    requests = []

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            requests.append((self.path, self.headers.get("Authorization")))
            self.send_response(status)
            if location:
                self.send_header("Location", location)
            self.end_headers()
            self.wfile.write(payload)

        def log_message(self, *args):
            pass

    class LocalServer(ThreadingHTTPServer):
        def server_bind(self):
            # Avoid macOS reverse-DNS lookup in HTTPServer.server_bind.
            TCPServer.server_bind(self)
            self.server_name = "localhost"
            self.server_port = self.server_address[1]

    server = LocalServer(("127.0.0.1", 0), Handler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}", requests
    finally:
        server.shutdown()
        server.server_close()
        thread.join(5)


def test_connectors_use_real_http_and_observer_only_for_crm(inventories):
    for source, payload, path in (("directory", inventories[0], "/snapshot"),
                                  ("crm", inventories[1], "/inventory")):
        with http_source(json.dumps(payload).encode()) as (url, requests):
            result = fetch_snapshot(source, url, "observer-test-token")
            assert result.source == source
            assert requests == [(path, "Bearer observer-test-token" if source == "crm" else None)]


@pytest.mark.parametrize("status,body,code", [(403, b"secret denial", "http_403"),
    (200, b"not json secret", "invalid_payload"), (200, b"{}", "invalid_payload"),
    (200, b"x" * (2 * 1024 * 1024 + 1), "oversized")])
def test_connector_errors_are_bounded_and_do_not_leak_payload(status, body, code):
    with http_source(body, status) as (url, _):
        with pytest.raises(ConnectorError) as caught:
            fetch_snapshot("crm", url, "observer-test-token")
    assert str(caught.value) == code


def test_connector_does_not_forward_credentials_to_redirect():
    with http_source(b"{}") as (target, received):
        with http_source(b"", 302, target) as (url, _):
            with pytest.raises(ConnectorError, match="http_302"):
                fetch_snapshot("crm", url, "observer-test-token")
        assert received == []


def test_unavailable_connector_returns_safe_error():
    import socket
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        with pytest.raises(ConnectorError, match="^unavailable$"):
            fetch_snapshot("directory", f"http://127.0.0.1:{sock.getsockname()[1]}")


def test_setup_is_private_repeatable_and_preserves_all_secrets(tmp_path):
    path = tmp_path / ".env"
    setup(path)
    first = path.read_text()
    setup(path)
    assert path.read_text() == first
    entries = dict(line.split("=", 1) for line in first.splitlines() if "=" in line)
    assert len(entries) == 5 and len(set(entries.values())) == 5
    assert len(entries["NEO4J_PASSWORD"]) >= 32
    assert path.stat().st_mode & 0o777 == 0o600
