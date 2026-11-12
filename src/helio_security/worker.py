"""Single-process observation worker with a durable queue and explicit ownership."""

import fcntl
import logging
import threading
import time

from .sync import sync_all
from .policies import policy
from .rules import RULE_ID

log = logging.getLogger(__name__)


class ObservationWorker:
    def __init__(self, store, graph, reader, directory_url, crm_url, observer_token,
                 *, interval=60, sync=sync_all, clock=time.time, workbench_url=None, workbench_token=None):
        self.store, self.graph, self.reader = store, graph, reader
        self.directory_url, self.crm_url, self.observer_token = directory_url, crm_url, observer_token
        self.sources = [("directory", directory_url, None), ("crm", crm_url, observer_token)]
        if workbench_url:
            self.sources.append(("workbench", workbench_url, workbench_token))
        self.interval, self.sync, self.clock = interval, sync, clock
        self._stop = threading.Event()
        self._thread = None
        self._lock = None
        self._next_sync = 0

    def start(self):
        lock = self.store.path.with_suffix(".worker.lock").open("a")
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            lock.close()
            raise RuntimeError("A security worker is already running for this database") from None
        try:
            self.store.recover(self.graph.company_id, self.clock())
            self._lock = lock
            self._stop.clear()
            self._thread = threading.Thread(target=self._loop, name="security-observer", daemon=True)
            self._thread.start()
        except Exception:
            lock.close()
            self._lock = None
            raise

    @property
    def alive(self):
        return self._thread is not None and self._thread.is_alive()

    def stop(self):
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=30)
            if self._thread.is_alive():
                raise RuntimeError("Observation worker did not finish shutdown")
        if self._lock:
            self._lock.close()
            self._lock = None

    def schedule(self, now):
        if self.interval and now >= self._next_sync:
            self.store.enqueue(self.graph.company_id, "scheduler", "scheduled", now)
            self._next_sync = now + self.interval

    def run_once(self):
        company = self.graph.company_id
        run = self.store.claim(company, self.clock())
        if run is None:
            return False
        results, evaluation, error = {}, None, None
        try:
            results = self.sync(self.graph, self.sources)
            evaluation = self.reader.evaluate(self.clock())
            for result in evaluation.get("evaluations", [evaluation]):
                if any(results.get(s) != "ok" for s in policy(result.get("rule", RULE_ID)).sources):
                    result["coverage_complete"] = False
        except Exception:
            # Worker boundary: persisted errors/logs never contain remote data,
            # tokens, exception messages or stack-local values.
            error = "observation_failed"
            log.warning("Observation run failed; retaining previous evidence")
        self.store.finish(company, run["id"], results, evaluation, self.clock(), error=error)
        return True

    def _loop(self):
        try:
            while not self._stop.is_set():
                self.schedule(self.clock())
                self.run_once()
                self._stop.wait(0.25)
        except Exception:
            # A storage failure stops processing; /health reports the dead worker.
            # On restart, the durable in-flight run is marked interrupted.
            log.error("Observation worker stopped because platform storage is unavailable")
