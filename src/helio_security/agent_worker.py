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
from .slack_app import LogNotifier, SlackConfig
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
        self._wake_event = None

    def _get_wake_event(self) -> asyncio.Event:
        if self._wake_event is None:
            self._wake_event = asyncio.Event()
        return self._wake_event

    def wake(self):
        if self._wake_event is not None:
            self._wake_event.set()

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
            except Exception:
                log.error("Error executing case %s; durable state retained for recovery", case_id)

    async def _deliver_decision(self, case_id: str, decision: dict):
        """At-least-once delivery; the checkpoint holds the durable receipt."""
        config = {"configurable": {"thread_id": case_id}}
        async with self._get_case_lock(case_id):
            try:
                snapshot = await self.graph.aget_state(config)
                if decision["id"] not in snapshot.values.get("processed_decisions", []):
                    if not snapshot.next:
                        return
                    # Resume a partly processed decision from its checkpoint,
                    # never inject it into the next human question.
                    in_progress = snapshot.values.get("human_input", {}).get("id") == decision["id"]
                    value = None if in_progress or snapshot.next != ("wait_human",) else Command(resume={
                        "id": decision["id"], "key": decision["key"],
                        "choice": decision["choice"], "employee_id": decision["employee_id"]})
                    await self.graph.ainvoke(value, config)
                    snapshot = await self.graph.aget_state(config)
                if decision["id"] in snapshot.values.get("processed_decisions", []):
                    self.case_store.consume_decision(case_id, decision["key"])
            except Exception:
                log.error("Decision delivery interrupted for case %s; will recover", case_id)

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
                    "human_override": self.case_store.has_human_override(self.company_id, fid),
                    "has_lead": True,
                    "addressed_to": "emp-hannah-reed",
                    "agent_id": evidence.get("agent_id", "agent:sales-research"),
                    "grant_id": evidence.get("grant_id"),
                },
            }
            await self._safe_run_case(case_id, state, config)

        # 2. Acknowledge decisions only after the graph stores their receipts.
        for case in self.case_store.cases_with_pending_decisions(self.company_id):
            for decision in self.case_store.unconsumed_decisions(case["id"]):
                await self._deliver_decision(case["id"], decision)

        # 3. Timeouts use the same durable delivery path as human answers. The
        # unique question key arbitrates a timeout racing a human response.
        for case in self.case_store.overdue_waiting_cases(self.company_id, now=now):
            config = {"configurable": {"thread_id": case["id"]}}
            snapshot = await self.graph.aget_state(config)
            question = snapshot.values.get("question", {})
            if snapshot.next != ("wait_human",) or not question.get("key"):
                continue
            self.case_store.record_decision(case["id"], key=question["key"], choice="timeout",
                employee_id="system", source="timeout", now=now)
            for decision in self.case_store.unconsumed_decisions(case["id"]):
                await self._deliver_decision(case["id"], decision)

        # 4. Recover every incomplete node, including notification failures that
        # already changed the case status to waiting but never reached interrupt.
        for case in self.case_store.active_cases(self.company_id):
            config = {"configurable": {"thread_id": case["id"]}}
            snapshot = await self.graph.aget_state(config)
            interrupted = any(task.interrupts for task in snapshot.tasks)
            if not snapshot.values:
                finding = self.platform_store.finding(self.company_id, case["finding_id"])
                if finding and finding.get("status") == "open" and finding.get("evidence_fresh"):
                    await self._safe_run_case(case["id"], {"finding_id": case["finding_id"],
                        "company_id": self.company_id}, config)
                else:
                    self.case_store.close_case(case["id"], status="needs_review", now=now)
            elif snapshot.next and not interrupted:
                await self._safe_run_case(case["id"], None, config)

        # 5. Flush outbox and write heartbeats
        if hasattr(self.deps.notifier, "flush"):
            try:
                await self.deps.notifier.flush()
            except Exception as e:
                log.warning("Notifier flush error: %s", e)

        self.platform_store.set_runtime(self.company_id, "agent_worker", {"status": "ok"}, now)
        if hasattr(self.deps.notifier, "delivery_status"):
            dstatus = self.deps.notifier.delivery_status()
            self.platform_store.set_runtime(
                self.company_id,
                "slack",
                {"status": "connected", "pending": dstatus.get("pending", 0), "unknown": dstatus.get("unknown", 0)},
                now,
            )

    async def run_forever(self):
        wake_event = self._get_wake_event()
        while not self._stop_event.is_set():
            try:
                await self.run_once()
            except Exception as e:
                log.exception("Error in agent worker cycle: %s", e)
            try:
                await asyncio.wait_for(wake_event.wait(), timeout=self.interval)
            except asyncio.TimeoutError:
                pass
            finally:
                wake_event.clear()


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
    try:
        from dotenv import load_dotenv
        load_dotenv()
    except ImportError:
        pass

    db_path = Path(os.getenv("SECURITY_DB_PATH", "data/security.sqlite3"))
    db_path.parent.mkdir(parents=True, exist_ok=True)
    checkpoint_db = os.getenv("AWASI_CHECKPOINT_DB", "data/checkpoints.sqlite3")
    Path(checkpoint_db).parent.mkdir(parents=True, exist_ok=True)
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

    slack_config = SlackConfig.from_environment()
    slack_handler = None

    def scripted_investigator(fid: str, facts: dict):
        agent_id = facts.get("agent_id", "Sales Research Agent")
        return {
            "summary": f"Agent {agent_id} retained customer database access after owner offboarded.",
            "confidence": "high",
            "source": "scripted",
        }

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    worker = None
    if slack_config.configured:
        log.info("Configuring Slack integration for channel %s", slack_config.channel)
        from slack_bolt.async_app import AsyncApp
        from slack_bolt.adapter.socket_mode.async_handler import AsyncSocketModeHandler
        from slack_sdk.web.async_client import AsyncWebClient
        from .slack_app import SlackActions, SlackNotifier, get_ssl_context

        ssl_ctx = get_ssl_context()
        bot_client = AsyncWebClient(token=slack_config.bot_token, ssl=ssl_ctx)
        slack_app = AsyncApp(client=bot_client)
        notifier = SlackNotifier(case_store, bot_client, slack_config)
        actions = SlackActions(
            case_store,
            notifier,
            slack_config,
            wake=lambda: loop.call_soon_threadsafe(worker.wake if worker else None),
        )

        @slack_app.action("awasi_choice")
        async def handle_choice(ack, body):
            await actions.handle(ack, body)

        @slack_app.action("awasi_undo")
        async def handle_undo(ack, body):
            await actions.handle(ack, body)

        app_client = AsyncWebClient(token=slack_config.app_token, ssl=ssl_ctx)
        slack_handler = AsyncSocketModeHandler(slack_app, slack_config.app_token, web_client=app_client, loop=loop)
    else:
        log.info("Slack is not configured; using LogNotifier")
        notifier = LogNotifier()

    deps = WorkflowDeps(
        store=case_store,
        checkpointer=checkpointer,
        notifier=notifier,
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

    def shutdown(sig, frame):
        log.info("Shutting down agent worker...")
        worker.stop()
        sys.exit(0)

    signal.signal(signal.SIGINT, shutdown)
    signal.signal(signal.SIGTERM, shutdown)

    async def runner():
        if slack_handler:
            try:
                await slack_handler.connect_async()
                log.info("Connected to Slack Socket Mode")
            except Exception as e:
                log.error("Failed to connect to Slack Socket Mode: %s", e)
        try:
            await worker.run_forever()
        finally:
            if slack_handler:
                try:
                    await slack_handler.close_async()
                except Exception:
                    pass

    log.info("Starting Awasi agent worker loop...")
    try:
        loop.run_until_complete(runner())
    finally:
        worker._release_lock()


if __name__ == "__main__":
    main()
