"""Single-process agent worker driving the case lifecycle loop.

Loops every 2 seconds:
1. Create a case for each open finding with fresh evidence and no active case, starting its thread.
2. Resume cases that have an unconsumed case_decisions row.
3. Resume waiting cases whose wait_until has passed with {"type": "timeout"}.
4. Re-invoke cases left mid-node by a crash with ainvoke(None, config).
5. Write a heartbeat with platform_store.set_runtime.
"""

import asyncio
import fcntl
import logging
import os
from pathlib import Path
import signal
import sys
import threading
import time
from typing import Dict, Optional

from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.types import Command

from .cases import CaseStore
from .platform_store import PlatformStore
from .playbooks import REGISTRY
from .workflow import WorkflowDeps, build_graph

log = logging.getLogger(__name__)


class AgentWorker:
    def __init__(
        self,
        platform_store: PlatformStore,
        case_store: CaseStore,
        deps: WorkflowDeps,
        graph=None,
        *,
        company_id: str = "helio",
        interval: float = 2.0,
        clock=time.time,
    ):
        self.platform_store = platform_store
        self.case_store = case_store
        self.deps = deps
        self.company_id = company_id
        self.interval = interval
        self.clock = clock
        self.graph = graph or build_graph(deps)

        self._case_locks: Dict[str, asyncio.Lock] = {}
        self._lock_file = None
        self._stop_event = threading.Event()
        self._thread = None

    def _get_case_lock(self, case_id: str) -> asyncio.Lock:
        if case_id not in self._case_locks:
            self._case_locks[case_id] = asyncio.Lock()
        return self._case_locks[case_id]

    def _acquire_lock(self):
        lock_path = self.platform_store.path.with_suffix(".agent.lock")
        lock_file = lock_path.open("a")
        try:
            fcntl.flock(lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
            self._lock_file = lock_file
        except BlockingIOError:
            lock_file.close()
            raise RuntimeError("An agent worker is already running for this database") from None

    def _release_lock(self):
        if self._lock_file:
            try:
                fcntl.flock(self._lock_file, fcntl.LOCK_UN)
                self._lock_file.close()
            except Exception:
                pass
            self._lock_file = None

    def start(self):
        self._acquire_lock()
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._run_loop, name="awasi-agent", daemon=True)
        self._thread.start()

    def stop(self):
        self._stop_event.set()
        if self._thread:
            self._thread.join(timeout=10)
        self._release_lock()

    @property
    def alive(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def _run_loop(self):
        asyncio.run(self.run_forever())

    async def _safe_run_case(self, case_id: str, input_val: any, config: dict):
        lock = self._get_case_lock(case_id)
        if lock.locked():
            return
        async with lock:
            try:
                await self.graph.ainvoke(input_val, config)
            except Exception as e:
                log.exception("Error executing case %s: %s", case_id, e)

    async def run_once(self, now: Optional[float] = None):
        now = self.clock() if now is None else now

        # 1. Create a case for each open finding with fresh evidence and no active case
        findings_page = self.platform_store.findings(self.company_id, status="open", now=now)
        open_findings = findings_page.get("items", [])
        fresh_open = [f for f in open_findings if f.get("evidence_fresh", True)]
        open_ids = [f["id"] for f in fresh_open]

        unhandled_ids = self.case_store.open_findings_without_case(self.company_id, open_finding_ids=open_ids)
        for fid in unhandled_ids:
            finding = next(f for f in fresh_open if f["id"] == fid)
            evidence = finding.get("evidence", {})
            case_id = self.case_store.open_case(
                self.company_id,
                finding_id=fid,
                route="investigating",
                now=now,
                addressed_to="emp-hannah-reed",
            )
            config = {"configurable": {"thread_id": case_id}}
            state = {
                "finding_id": fid,
                "company_id": self.company_id,
                "facts": {
                    "rule": finding.get("rule", "inactive_owner_agent_access"),
                    "target_kind": "agent",
                    "owner_status": evidence.get("employee_status", "inactive"),
                    "data_classification": evidence.get("classification", "confidential"),
                    "record_count": evidence.get("record_count", 6),
                    "exposure": "none",
                    "leaking_now": False,
                    "dependents": 0,
                    "human_override": False,
                    "has_lead": True,
                    "addressed_to": "emp-hannah-reed",
                    "agent_id": evidence.get("agent_id", "agent:sales-research"),
                    "grant_id": evidence.get("grant_id"),
                },
            }
            await self._safe_run_case(case_id, state, config)

        # 2. Resume cases that have an unconsumed case_decisions row
        cases_with_decisions = self.case_store.cases_with_pending_decisions(self.company_id)
        for case in cases_with_decisions:
            case_id = case["id"]
            pending = self.case_store.unconsumed_decisions(case_id)
            for dec in pending:
                consumed = self.case_store.consume_decision(case_id, dec["key"])
                if consumed:
                    config = {"configurable": {"thread_id": case_id}}
                    resume_cmd = Command(resume={"choice": dec["choice"], "employee_id": dec["employee_id"]})
                    await self._safe_run_case(case_id, resume_cmd, config)

        # 3. Resume waiting cases whose wait_until has passed with {"type": "timeout"}
        overdue = self.case_store.overdue_waiting_cases(self.company_id, now=now)
        for case in overdue:
            case_id = case["id"]
            # Clear wait_until so it doesn't fire repeatedly
            self.case_store.set_wait_until(case_id, 0.0)
            config = {"configurable": {"thread_id": case_id}}
            resume_cmd = Command(resume={"choice": "timeout", "type": "timeout", "employee_id": "system"})
            await self._safe_run_case(case_id, resume_cmd, config)

        # 4. Re-invoke cases left mid-node by a crash
        active_cases = self.case_store.active_cases(self.company_id)
        for case in active_cases:
            if case["status"] in ("investigating", "fixing", "verifying"):
                case_id = case["id"]
                lock = self._get_case_lock(case_id)
                if not lock.locked():
                    config = {"configurable": {"thread_id": case_id}}
                    await self._safe_run_case(case_id, None, config)

        # 5. Write a heartbeat with platform_store.set_runtime
        self.platform_store.set_runtime(self.company_id, "agent_worker", {"status": "ok"}, now)

    async def run_forever(self):
        while not self._stop_event.is_set():
            try:
                await self.run_once()
            except Exception as e:
                log.exception("Error in agent worker cycle: %s", e)
            await asyncio.sleep(self.interval)


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
    db_path = Path(os.getenv("SECURITY_DB_PATH", "data/security.sqlite3"))
    checkpoint_db = os.getenv("AWASI_CHECKPOINT_DB", "/data/checkpoints.sqlite3")
    company_id = os.getenv("HELIO_COMPANY_ID", "helio")
    crm_url = os.getenv("CRM_URL", "http://crm:8001")
    action_token = os.getenv("CRM_ACTION_TOKEN", "")
    observer_token = os.getenv("CRM_OBSERVER_TOKEN", "")
    deadline_sec = int(os.getenv("AWASI_OWNER_DEADLINE_SECONDS", "900"))
    verify_timeout_sec = float(os.getenv("AWASI_VERIFY_TIMEOUT_SECONDS", "60.0"))

    platform_store = PlatformStore(db_path)
    platform_store.initialize()

    case_store = CaseStore(db_path)
    case_store.initialize()

    checkpointer = SqliteSaver.from_conn_string(checkpoint_db)

    class StdoutNotifier:
        async def notify(self, case_id: str, event_type: str, payload: dict):
            log.info("CASE %s [%s]: %s", case_id, event_type, payload)

    def scripted_investigator(fid: str, facts: dict):
        agent_id = facts.get("agent_id", "Sales Research Agent")
        return {
            "summary": f"Agent {agent_id} retained customer database access after owner offboarded.",
            "confidence": "high",
            "source": "scripted",
        }

    deps = WorkflowDeps(
        store=case_store,
        checkpointer=checkpointer,
        notifier=StdoutNotifier(),
        investigator=scripted_investigator,
        playbooks=REGISTRY,
        company_id=company_id,
        owner_deadline_seconds=deadline_sec,
        platform_store=platform_store,
        crm_url=crm_url,
        action_token=action_token,
        observer_token=observer_token,
        verify_timeout_seconds=verify_timeout_sec,
    )

    worker = AgentWorker(
        platform_store=platform_store,
        case_store=case_store,
        deps=deps,
        company_id=company_id,
        interval=2.0,
    )

    worker._acquire_lock()

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    def shutdown(sig, frame):
        log.info("Shutting down agent worker...")
        worker.stop()
        sys.exit(0)

    signal.signal(signal.SIGINT, shutdown)
    signal.signal(signal.SIGTERM, shutdown)

    log.info("Starting Awasi agent worker loop...")
    try:
        loop.run_until_complete(worker.run_forever())
    finally:
        worker._release_lock()


if __name__ == "__main__":
    main()
