import json
from unittest.mock import MagicMock

import pytest
from neo4j.exceptions import ServiceUnavailable

from helio_security import cli


def test_missing_password_has_safe_message_and_nonzero_exit(monkeypatch, capsys):
    monkeypatch.setattr("sys.argv", ["graph", "sync"])
    monkeypatch.delenv("NEO4J_PASSWORD", raising=False)
    assert cli.main() == 1
    assert "configuration invalid" in json.loads(capsys.readouterr().out)["error"]


def test_connection_exception_does_not_expose_details(monkeypatch, capsys):
    monkeypatch.setattr("sys.argv", ["graph", "status"])
    monkeypatch.setenv("NEO4J_PASSWORD", "secret-do-not-print")
    factory = MagicMock(side_effect=ServiceUnavailable("secret-do-not-print"))
    monkeypatch.setattr(cli.GraphDatabase, "driver", factory)
    assert cli.main() == 1
    output = capsys.readouterr().out
    assert "secret-do-not-print" not in output
    assert "error" in json.loads(output)


@pytest.mark.parametrize("sync_result,exit_code", [
    ({"directory": "ok", "crm": "ok"}, 0),
    ({"directory": "ok", "crm": "http_403"}, 1),
    ({"directory": "ok", "crm": "skipped_older"}, 1),
])
def test_sync_exit_code_reflects_partial_or_skipped_observations(monkeypatch, capsys, sync_result, exit_code):
    monkeypatch.setattr("sys.argv", ["graph", "sync"])
    monkeypatch.setenv("NEO4J_PASSWORD", "secret-do-not-print")
    monkeypatch.setattr(cli.GraphDatabase, "driver", MagicMock())
    graph = MagicMock()
    graph.status.return_value = {"company_id": "helio"}
    monkeypatch.setattr(cli, "GraphStore", MagicMock(return_value=graph))
    monkeypatch.setattr(cli, "sync_all", MagicMock(return_value=sync_result))
    assert cli.main() == exit_code
    assert json.loads(capsys.readouterr().out)["sync"] == sync_result


@pytest.mark.parametrize("command", ["status", "risks"])
def test_read_commands_do_not_sync_or_initialize(monkeypatch, capsys, command):
    monkeypatch.setattr("sys.argv", ["graph", command])
    monkeypatch.setenv("NEO4J_PASSWORD", "secret-do-not-print")
    monkeypatch.setattr(cli.GraphDatabase, "driver", MagicMock())
    graph = MagicMock()
    getattr(graph, command).return_value = {"company_id": "helio"}
    monkeypatch.setattr(cli, "GraphStore", MagicMock(return_value=graph))
    sync = MagicMock()
    monkeypatch.setattr(cli, "sync_all", sync)
    assert cli.main() == 0
    assert json.loads(capsys.readouterr().out) == {"company_id": "helio"}
    graph.initialize.assert_not_called()
    sync.assert_not_called()
