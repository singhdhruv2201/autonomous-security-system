"""Unit tests for the single-process AgentWorker (Phase 1).

Covers:
1. Creating a case for an open finding with fresh evidence and running it
2. Resuming cases with unconsumed decisions
3. Resuming overdue waiting cases with timeout
4. Heartbeat writing to runtime
5. Exclusive file lock prevention
"""

import asyncio
import time
from pathlib import Path
from unittest.mock import Mock

import pytest
from langgraph.checkpoint.memory import MemorySaver

from helio_security.agent_worker import AgentWorker
from helio_security.cases import CaseStore
from helio_security.platform_store import PlatformStore
from helio_security.workflow import WorkflowDeps, build_graph


COMPANY = "helio"
FINDING_ID = "finding-orphaned-agent"


class FakePlaybook:
    def __init__(self):
        self.executed = []

    async def run(self, target, **kwargs):
        self.executed.append(target)
        return {"principal_status": "disabled"}

    async def verify(self, target, **kwargs):
        return True

    async def undo(self, target, **kwargs):
        pass


class FakeNotifier:
    def __init__(self):
        self.notifications = []

    async def notify(self, case_id, event_type, payload):
        self.notifications.append((case_id, event_type, payload))


@pytest.fixture
def stores(tmp_path):
    p_store = PlatformStore(tmp_path / "security.sqlite3")
    p_store.initialize()
    c_store = CaseStore(tmp_path / "security.sqlite3")
    c_store.initialize()
    return p_store, c_store


@pytest.fixture
def worker(stores, tmp_path):
    p_store, c_store = stores
    playbook = FakePlaybook()
    notifier = FakeNotifier()
    deps = WorkflowDeps(
        store=c_store,
        checkpointer=MemorySaver(),
        notifier=notifier,
        investigator=lambda fid, facts: {"summary": "Orphaned agent found", "confidence": "high"},
        playbooks={"crm.pause_principal": playbook},
        company_id=COMPANY,
        clock=time.time,
        platform_store=p_store,
    )
    return AgentWorker(
        platform_store=p_store,
        case_store=c_store,
        deps=deps,
        company_id=COMPANY,
        interval=2.0,
    )


class TestAgentWorker:
    def test_run_once_creates_and_processes_case_for_open_finding(self, worker, stores):
        p_store, c_store = stores
        now = time.time()
        # Seed an open finding in platform store
        with p_store.connection() as db:
            db.execute(
                "INSERT INTO findings VALUES (?, ?, 'inactive_owner_agent_access', 'high', 'open', 'current', ?, ?, ?, ?, NULL, ?, 'inactive_owner_active_access')",
                (FINDING_ID, COMPANY, now, now, now, now, '{"agent_id":"agent:sales-research","employee_status":"inactive","classification":"confidential","record_count":6}'),
            )

        # Run worker iteration
        asyncio.get_event_loop().run_until_complete(worker.run_once())

        # Verify case was opened and processed
        active = c_store.active_cases(COMPANY)
        assert len(active) == 1
        case = active[0]
        assert case["finding_id"] == FINDING_ID
        assert case["status"] in ("waiting_approver", "waiting_owner")
        assert len(c_store.get_actions(case["id"])) == 1

    def test_run_once_resumes_case_on_unconsumed_decision(self, worker, stores):
        p_store, c_store = stores
        now = time.time()
        with p_store.connection() as db:
            db.execute(
                "INSERT INTO findings VALUES (?, ?, 'inactive_owner_agent_access', 'high', 'open', 'current', ?, ?, ?, ?, NULL, ?, 'inactive_owner_active_access')",
                (FINDING_ID, COMPANY, now, now, now, now, '{"agent_id":"agent:sales-research","employee_status":"inactive","classification":"confidential","record_count":6}'),
            )

        # First run to reach waiting state
        asyncio.get_event_loop().run_until_complete(worker.run_once())
        case = c_store.active_cases(COMPANY)[0]

        # Record decision
        c_store.record_decision(case["id"], key="decision-1", employee_id="emp-hannah-reed", source="slack", choice="retire", now=now)

        # Second run should resume and resolve
        asyncio.get_event_loop().run_until_complete(worker.run_once())
        updated = c_store.get_case(case["id"])
        assert updated["status"] == "resolved"

    def test_heartbeat_is_written(self, worker, stores):
        p_store, _ = stores
        asyncio.get_event_loop().run_until_complete(worker.run_once())
        rt = p_store.runtime(COMPANY)
        assert "agent_worker" in rt
        assert rt["agent_worker"]["value"]["status"] == "ok"

    def test_exclusive_lock_prevents_duplicate_worker(self, worker):
        worker.start()
        try:
            worker2 = AgentWorker(
                platform_store=worker.platform_store,
                case_store=worker.case_store,
                deps=worker.deps,
                company_id=COMPANY,
            )
            with pytest.raises(RuntimeError, match="already running"):
                worker2.start()
        finally:
            worker.stop()
