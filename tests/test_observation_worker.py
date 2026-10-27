from unittest.mock import Mock

import pytest

from helio_security.platform_store import PlatformStore
from helio_security.worker import ObservationWorker
from test_platform_store import evaluation


@pytest.fixture
def worker(tmp_path):
    store = PlatformStore(tmp_path / "security.sqlite3")
    store.initialize()
    graph = Mock(company_id="helio")
    reader = Mock()
    reader.evaluate.return_value = evaluation()
    sync = Mock(return_value={"directory": "ok", "crm": "ok"})
    return ObservationWorker(store, graph, reader, "http://directory", "http://crm", "observer-only",
                             interval=0, sync=sync, clock=lambda: 100)


def test_worker_observes_and_atomically_records_findings_and_run(worker):
    run, _ = worker.store.enqueue("helio", "operator", "manual", 99)
    assert worker.run_once() is True
    assert worker.store.run("helio", run["id"])["status"] == "succeeded"
    assert worker.store.findings("helio", now=100)["total"] == 1
    worker.sync.assert_called_once_with(worker.graph, "http://directory", "http://crm", "observer-only")
    assert worker.run_once() is False


def test_worker_failure_is_persisted_without_exception_or_secret(worker):
    worker.sync.side_effect = RuntimeError("secret-token-and-payload")
    run, _ = worker.store.enqueue("helio", "operator", "manual", 99)
    worker.run_once()
    record = worker.store.run("helio", run["id"])
    assert record["status"] == "failed" and record["error"] == "observation_failed"
    assert "secret-token" not in str(record)


def test_worker_partial_sync_cannot_use_old_success_to_resolve_findings(worker):
    worker.sync.return_value = {"directory": "ok", "crm": "skipped_older"}
    run, _ = worker.store.enqueue("helio", "operator", "manual", 99)
    worker.run_once()
    assert worker.store.run("helio", run["id"])["status"] == "partial"
    assert worker.store.findings("helio", now=100)["total"] == 0


def test_only_one_process_worker_can_own_a_platform_database(worker):
    second = ObservationWorker(worker.store, worker.graph, worker.reader, "directory", "crm", "observer", interval=0)
    worker.start()
    try:
        with pytest.raises(RuntimeError, match="already running"):
            second.start()
    finally:
        worker.stop()
    second.start()
    second.stop()


def test_schedule_enqueues_without_overlapping_an_active_run(worker):
    worker.interval = 60
    worker.schedule(100)
    worker.schedule(101)
    worker.schedule(160)
    assert worker.store.runs("helio")["total"] == 1
    worker.run_once()
    worker.schedule(220)
    assert worker.store.runs("helio")["total"] == 2
    assert worker.store.runs("helio")["items"][0]["trigger"] == "scheduled"
