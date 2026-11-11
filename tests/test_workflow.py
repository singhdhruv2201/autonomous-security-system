"""Tests for the LangGraph workflow skeleton (Phase 1).

TDD: written first. All routes use fakes — no model, no Slack.
Uses InMemorySaver for the in-memory path, SqliteSaver for persistence.
"""

import asyncio
import secrets
import time
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest
from langgraph.checkpoint.memory import MemorySaver

from helio_security.cases import CaseStore
from helio_security.decision import Facts, Decision, decide
from helio_security.workflow import build_graph, WorkflowDeps


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

COMPANY = "helio"
FINDING_ID = "finding-aaa"


def make_facts(**overrides):
    base = dict(
        rule="inactive_owner_agent_access",
        target_kind="agent",
        owner_status="inactive",
        data_classification="confidential",
        record_count=6,
        exposure="none",
        leaking_now=False,
        dependents=0,
        human_override=False,
        has_lead=True,
        addressed_to="emp-hannah-reed",
        agent_id="agent:sales-research",
        grant_id="grant-sales-agent",
    )
    base.update(overrides)
    return base


def scripted_investigator(finding, facts):
    """Synchronous scripted investigator that returns a canned summary."""
    return {
        "summary": f"Agent {facts.get('agent_id', 'unknown')} retained access after owner offboarded.",
        "confidence": "high",
        "source": "scripted",
    }


class FakePlaybook:
    def __init__(self, *, reversible=True, fail=False, verify_ok=True):
        self.reversible = reversible
        self.fail = fail
        self.verify_ok = verify_ok
        self.executed = []
        self.verified = []
        self.undone = []

    async def read(self, target):
        return {"principal_status": "active", "access_version": 1}

    async def run(self, target, before_state):
        if self.fail:
            raise RuntimeError("Playbook failed")
        self.executed.append(target)
        return {"principal_status": "disabled"}

    async def undo(self, target, before):
        self.undone.append((target, before))

    async def verify(self, target):
        self.verified.append(target)
        return self.verify_ok


class LogNotifier:
    def __init__(self):
        self.messages = []

    async def notify(self, case_id, event_type, payload):
        self.messages.append((case_id, event_type, payload))


def make_deps(tmp_path, *, playbook=None, investigator=None, notifier=None):
    db_path = tmp_path / "security.sqlite3"
    store = CaseStore(db_path)
    store.initialize()
    checkpointer = MemorySaver()
    return WorkflowDeps(
        store=store,
        checkpointer=checkpointer,
        notifier=notifier or LogNotifier(),
        investigator=investigator or scripted_investigator,
        playbooks={"crm.pause_principal": playbook or FakePlaybook()},
        clock=time.time,
        company_id=COMPANY,
        owner_deadline_seconds=900,
    )


# ---------------------------------------------------------------------------
# End-state tests
# ---------------------------------------------------------------------------

class TestWorkflowRoutes:
    def test_fix_then_ask_lead_reaches_waiting_human(self, tmp_path):
        """inactive owner, no dependents → fix runs, then pauses at wait_human."""
        deps = make_deps(tmp_path)
        graph = build_graph(deps)
        case_id = deps.store.open_case(COMPANY, FINDING_ID, route="fix_then_ask", now=time.time())
        config = {"configurable": {"thread_id": case_id}}
        state = {"finding_id": FINDING_ID, "company_id": COMPANY, "facts": make_facts()}
        result = asyncio.run(graph.ainvoke(state, config))
        # Should be interrupted at wait_human
        interrupts = result.get("__interrupt__", [])
        assert len(interrupts) == 1 or deps.store.get_case(case_id)["status"] in (
            "waiting_owner", "waiting_approver"
        )

    def test_no_dependents_runs_playbook(self, tmp_path):
        pb = FakePlaybook()
        deps = make_deps(tmp_path, playbook=pb)
        graph = build_graph(deps)
        case_id = deps.store.open_case(COMPANY, FINDING_ID, route="fix_then_ask", now=time.time())
        config = {"configurable": {"thread_id": case_id}}
        state = {"finding_id": FINDING_ID, "company_id": COMPANY, "facts": make_facts()}
        asyncio.run(graph.ainvoke(state, config))
        assert len(pb.executed) == 1

    def test_unverified_playbook_ends_as_needs_review(self, tmp_path):
        pb = FakePlaybook(verify_ok=False)
        deps = make_deps(tmp_path, playbook=pb)
        graph = build_graph(deps)
        case_id = deps.store.open_case(COMPANY, FINDING_ID, route="fix_then_ask", now=time.time())
        config = {"configurable": {"thread_id": case_id}}
        state = {"finding_id": FINDING_ID, "company_id": COMPANY, "facts": make_facts()}
        asyncio.run(graph.ainvoke(state, config))
        case = deps.store.get_case(case_id)
        assert case["status"] == "needs_review"

    def test_verify_enqueues_sync_run(self, tmp_path):
        from helio_security.platform_store import PlatformStore
        p_store = PlatformStore(tmp_path / "security_p.sqlite3")
        p_store.initialize()
        pb = FakePlaybook(verify_ok=True)
        deps = make_deps(tmp_path, playbook=pb)
        deps.platform_store = p_store
        graph = build_graph(deps)
        case_id = deps.store.open_case(COMPANY, FINDING_ID, route="fix_then_ask", now=time.time())
        config = {"configurable": {"thread_id": case_id}}
        state = {"finding_id": FINDING_ID, "company_id": COMPANY, "facts": make_facts()}
        asyncio.run(graph.ainvoke(state, config))
        # Platform store should have an enqueued sync run
        runs = p_store.runs(COMPANY)
        assert len(runs["items"]) >= 1

    def test_events_not_duplicated_on_rerun(self, tmp_path):
        """Running the same case twice must not add duplicate events."""
        deps = make_deps(tmp_path)
        graph = build_graph(deps)
        case_id = deps.store.open_case(COMPANY, FINDING_ID, route="fix_then_ask", now=time.time())
        config = {"configurable": {"thread_id": case_id}}
        state = {"finding_id": FINDING_ID, "company_id": COMPANY, "facts": make_facts()}
        for _ in range(2):
            asyncio.run(graph.ainvoke(state, config))
        events = deps.store.get_events(case_id)
        keys = [e["key"] for e in events]
        assert len(keys) == len(set(keys)), "Duplicate event keys found"

    def test_actions_not_duplicated_on_rerun(self, tmp_path):
        """Running the same case twice must not create duplicate action rows."""
        pb = FakePlaybook()
        deps = make_deps(tmp_path, playbook=pb)
        graph = build_graph(deps)
        case_id = deps.store.open_case(COMPANY, FINDING_ID, route="fix_then_ask", now=time.time())
        config = {"configurable": {"thread_id": case_id}}
        state = {"finding_id": FINDING_ID, "company_id": COMPANY, "facts": make_facts()}
        for _ in range(2):
            asyncio.run(graph.ainvoke(state, config))
        actions = deps.store.get_actions(case_id)
        # Idempotency key ensures only one row
        assert len(actions) == 1
        # But real execution should only happen once
        assert len(pb.executed) == 1


class TestWorkflowResume:
    def test_case_resumes_after_decision(self, tmp_path):
        """A case paused at wait_human resumes when a decision is recorded."""
        deps = make_deps(tmp_path)
        graph = build_graph(deps)
        case_id = deps.store.open_case(COMPANY, FINDING_ID, route="fix_then_ask", now=time.time())
        config = {"configurable": {"thread_id": case_id}}
        state = {"finding_id": FINDING_ID, "company_id": COMPANY, "facts": make_facts()}
        # First run — should stop at wait_human
        asyncio.run(graph.ainvoke(state, config))
        # Record a decision and resume
        from langgraph.types import Command
        decision_payload = {"choice": "retire", "employee_id": "emp-hannah-reed"}
        result2 = asyncio.run(
            graph.ainvoke(Command(resume=decision_payload), config)
        )
        case = deps.store.get_case(case_id)
        assert case["status"] in ("resolved", "needs_review", "waiting_approver")

    def test_sqlite_checkpointer_persists_across_rebuild(self, tmp_path):
        """A waiting case survives graph rebuild (simulates process restart)."""
        from langgraph.checkpoint.sqlite import SqliteSaver
        checkpoint_path = str(tmp_path / "checkpoints.sqlite3")
        db_path = tmp_path / "security.sqlite3"
        store = CaseStore(db_path)
        store.initialize()

        def make_graph_with_sqlite():
            checkpointer = SqliteSaver.from_conn_string(checkpoint_path)
            d = WorkflowDeps(
                store=store,
                checkpointer=checkpointer,
                notifier=LogNotifier(),
                investigator=scripted_investigator,
                playbooks={"crm.pause_principal": FakePlaybook()},
                clock=time.time,
                company_id=COMPANY,
                owner_deadline_seconds=900,
            )
            return build_graph(d), d

        graph1, deps1 = make_graph_with_sqlite()
        case_id = deps1.store.open_case(COMPANY, FINDING_ID, route="fix_then_ask", now=time.time())
        config = {"configurable": {"thread_id": case_id}}
        state = {"finding_id": FINDING_ID, "company_id": COMPANY, "facts": make_facts()}
        asyncio.run(graph1.ainvoke(state, config))

        # Rebuild graph (simulates restart)
        graph2, deps2 = make_graph_with_sqlite()
        # Resume with a decision
        from langgraph.types import Command
        asyncio.run(
            graph2.ainvoke(Command(resume={"choice": "retire", "employee_id": "emp-hannah-reed"}), config)
        )
        case = deps2.store.get_case(case_id)
        assert case["status"] in ("resolved", "needs_review", "waiting_approver")
