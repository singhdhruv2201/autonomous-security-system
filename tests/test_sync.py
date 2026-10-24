from unittest.mock import Mock

from helio_security.connectors import ConnectorError
from helio_security.sync import sync_all


def test_sync_continues_after_source_failure_and_records_safe_error():
    graph = Mock()
    graph.apply.return_value = True
    snapshot = object()
    fetch = Mock(side_effect=[ConnectorError("unavailable"), snapshot])
    ticks = iter([10.0, 11.0])
    result = sync_all(graph, "http://directory", "http://crm", "observer", fetch=fetch,
                      clock=lambda: next(ticks))
    graph.fail.assert_called_once_with("directory", "unavailable", 10.0)
    graph.apply.assert_called_once_with(snapshot, 11.0)
    assert result == {"directory": "unavailable", "crm": "ok"}
    assert fetch.call_args_list[0].args == ("directory", "http://directory", None)
    assert fetch.call_args_list[1].args == ("crm", "http://crm", "observer")


def test_invalid_graph_import_reports_failure_without_leaking_exception():
    graph = Mock()
    graph.apply.side_effect = ValueError("Sensitive payload must not appear")
    result = sync_all(graph, "directory", "crm", "observer", fetch=Mock(return_value=object()), clock=lambda: 1.0)
    assert result == {"directory": "import_failed", "crm": "import_failed"}
    assert all(call.args[1] == "import_failed" for call in graph.fail.call_args_list)


def test_older_snapshot_reports_skip():
    graph = Mock()
    graph.apply.return_value = False
    result = sync_all(graph, "directory", "crm", "observer", fetch=Mock(return_value=object()), clock=lambda: 1.0)
    assert result == {"directory": "skipped_older", "crm": "skipped_older"}
