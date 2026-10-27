import json

import pytest

from helio_security.client import main
from test_security import http_source


@pytest.mark.parametrize("command,path,token", [("overview", "/overview", "viewer"),
    ("findings", "/findings", "viewer"), ("connectors", "/connectors", "viewer"),
    ("runs", "/sync-runs", "viewer"), ("audit", "/audit", "operator")])
def test_client_reads_api_using_least_privileged_token(monkeypatch, capsys, command, path, token):
    with http_source(b'{"status":"ok"}') as (url, requests):
        monkeypatch.setenv("SECURITY_API_URL", url)
        monkeypatch.setenv("SECURITY_OPERATOR_TOKEN", "operator")
        monkeypatch.setenv("SECURITY_VIEWER_TOKEN", "viewer")
        monkeypatch.setattr("sys.argv", ["security", command])
        assert main() == 0
        assert requests == [(path, "Bearer " + token)]
        assert json.loads(capsys.readouterr().out) == {"status": "ok"}


def test_client_reports_safe_auth_error(monkeypatch, capsys):
    with http_source(b"do-not-print-secret", status=403) as (url, _):
        monkeypatch.setenv("SECURITY_API_URL", url)
        monkeypatch.setenv("SECURITY_VIEWER_TOKEN", "viewer-secret")
        monkeypatch.setattr("sys.argv", ["security", "overview"])
        assert main() == 2
        output = capsys.readouterr().out
        assert "403" in output and "secret" not in output


def test_client_sync_posts_a_run(monkeypatch, capsys):
    from unittest.mock import MagicMock
    import io
    monkeypatch.setenv("SECURITY_OPERATOR_TOKEN", "operator")
    monkeypatch.setattr("sys.argv", ["security", "sync"])
    opener = MagicMock(return_value=io.BytesIO(b'{"id":"run-1","status":"queued"}'))
    monkeypatch.setattr("helio_security.client.urllib.request.urlopen", opener)
    assert main() == 0
    req = opener.call_args.args[0]
    assert req.method == "POST" and req.data == b"{}"
    assert req.get_header("Authorization") == "Bearer operator"
    assert json.loads(capsys.readouterr().out)["status"] == "queued"
