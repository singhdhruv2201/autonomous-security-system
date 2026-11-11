"""Regression tests for durable actions, decision delivery and truthful outcomes."""
import asyncio
import time

import pytest
from langgraph.types import Command
from langgraph.checkpoint.sqlite import SqliteSaver

from helio_security.agent_worker import AgentWorker
from helio_security.platform_store import PlatformStore
from helio_security.workflow import build_graph
from test_workflow import make_deps, make_facts


class Crash(BaseException):
    """Simulate process loss without running application error handlers."""


class StatefulPlaybook:
    reversible = True

    def __init__(self):
        self.status = "active"
        self.calls = 0
        self.undo_calls = 0
        self.fail = False

    async def read(self, target):
        return {"principal_status": self.status, "access_version": 1}

    async def run(self, target, before_state=None):
        self.calls += 1
        if self.fail:
            raise TimeoutError("ambiguous dispatch")
        self.status = "disabled"
        return {"status": "executed", "before": before_state}

    async def verify(self, target, expected_state="disabled"):
        return self.status == expected_state

    async def undo(self, target, before, **kwargs):
        self.undo_calls += 1
        self.status = before["principal_status"]
        return {"status": "executed"}


def setup_case(tmp_path, *, facts=None, pb=None, sqlite=False):
    pb = pb or StatefulPlaybook()
    deps = make_deps(tmp_path, playbook=pb)
    if sqlite:
        deps.checkpointer = SqliteSaver.from_conn_string(str(tmp_path / "checkpoints.sqlite3"))
    graph = build_graph(deps)
    cid = deps.store.open_case("helio", "finding-aaa", route="investigating", now=time.time())
    config = {"configurable": {"thread_id": cid}}
    state = {"finding_id": "finding-aaa", "company_id": "helio", "facts": make_facts(
        agent_id="agent:sales-research", grant_id="grant-sales-agent", **(facts or {}))}
    return deps, graph, cid, config, state, pb


def test_crash_after_remote_commit_reconciles_without_repeat_and_preserves_before(tmp_path, monkeypatch):
    deps, graph, cid, config, state, pb = setup_case(tmp_path, sqlite=True)
    original = deps.store.update_action
    def crash_on_result(*args, **kwargs):
        if kwargs.get("status") == "executed":
            raise Crash()
        return original(*args, **kwargs)
    monkeypatch.setattr(deps.store, "update_action", crash_on_result)
    with pytest.raises(Crash):
        asyncio.run(graph.ainvoke(state, config))
    action = deps.store.get_actions(cid)[0]
    assert action["before"]["principal_status"] == "active"
    assert action["dispatched_at"] is not None
    monkeypatch.setattr(deps.store, "update_action", original)
    deps.checkpointer = SqliteSaver.from_conn_string(str(tmp_path / "checkpoints.sqlite3"))
    graph = build_graph(deps)
    asyncio.run(graph.ainvoke(None, config))
    assert pb.calls == 1
    action = deps.store.get_actions(cid)[0]
    assert action["before"]["principal_status"] == "active"
    assert action["status"] == "executed"
    assert action["verified_at"] is not None


def test_ambiguous_action_stops_for_review_without_retries(tmp_path):
    pb = StatefulPlaybook(); pb.fail = True
    deps, graph, cid, config, state, _ = setup_case(tmp_path, pb=pb)
    asyncio.run(graph.ainvoke(state, config))
    asyncio.run(graph.ainvoke(None, config))
    assert deps.store.get_case(cid)["status"] == "needs_review"
    assert deps.store.get_actions(cid)[0]["status"] == "unknown"
    assert pb.calls == 1
    assert deps.store.open_findings_without_case("helio", open_finding_ids=["finding-aaa"]) == []


@pytest.mark.parametrize("choice", ["timeout", "reject", "not-an-offered-choice"])
def test_unapproved_outcomes_never_resolve(tmp_path, choice):
    deps, graph, cid, config, state, pb = setup_case(tmp_path, facts={"dependents": 1})
    asyncio.run(graph.ainvoke(state, config))
    asyncio.run(graph.ainvoke(Command(resume={"choice": choice, "employee_id": "operator"}), config))
    assert deps.store.get_case(cid)["status"] == "needs_review"
    assert pb.calls == 0


def test_approval_executes_and_verifies_proposed_action(tmp_path):
    deps, graph, cid, config, state, pb = setup_case(tmp_path)
    deps.investigator = lambda *_: {"summary": "Needs approval", "confidence": "low"}
    asyncio.run(graph.ainvoke(state, config))
    assert pb.calls == 0
    asyncio.run(graph.ainvoke(Command(resume={"choice": "approve", "employee_id": "emp-casey-wong"}), config))
    assert pb.calls == 1
    assert deps.store.get_case(cid)["status"] == "resolved"
    assert deps.store.get_actions(cid)[0]["verified_at"] is not None


def test_retire_requires_casey_and_targets_grant(tmp_path):
    deps, graph, cid, config, state, pb = setup_case(tmp_path)
    revoke = StatefulPlaybook()
    targets = []
    original = revoke.run
    async def run(target, before_state=None):
        targets.append(target)
        return await original(target, before_state)
    revoke.run = run
    deps.playbooks["crm.revoke_grant"] = revoke
    asyncio.run(graph.ainvoke(state, config))
    asyncio.run(graph.ainvoke(Command(resume={"choice": "retire", "employee_id": "emp-hannah-reed"}), config))
    assert deps.store.get_case(cid)["status"] == "waiting_approver"
    assert deps.store.get_case(cid)["addressed_to"] == "emp-casey-wong"
    assert targets == []
    asyncio.run(graph.ainvoke(Command(resume={"choice": "approve", "employee_id": "emp-casey-wong"}), config))
    assert targets == ["grant-sales-agent"]
    assert deps.store.get_case(cid)["status"] == "resolved"


def test_undo_persists_override_and_prevents_next_automatic_pause(tmp_path):
    deps, graph, cid, config, state, pb = setup_case(tmp_path)
    asyncio.run(graph.ainvoke(state, config))
    asyncio.run(graph.ainvoke(Command(resume={"choice": "undo", "employee_id": "operator"}), config))
    assert pb.status == "active"
    actions = deps.store.get_actions(cid)
    assert actions[0]["status"] == "undone"
    assert any(a["undo_of"] == actions[0]["id"] for a in actions)
    new_id = deps.store.open_case("helio", "finding-aaa", route="investigating", now=time.time())
    asyncio.run(graph.ainvoke(state, {"configurable": {"thread_id": new_id}}))
    assert pb.calls == 1
    assert deps.store.get_case(new_id)["addressed_to"] == "emp-casey-wong"


@pytest.mark.parametrize("fail_after_checkpoint", [False, True])
def test_worker_retries_decision_delivery_without_reapplying_it(tmp_path, fail_after_checkpoint):
    deps, graph, cid, config, state, pb = setup_case(tmp_path, sqlite=True)
    asyncio.run(graph.ainvoke(state, config))
    deps.store.record_decision(cid, key="lead_decision", choice="keep", employee_id="operator", source="api", now=time.time())
    platform = PlatformStore(tmp_path / "platform.sqlite3"); platform.initialize()
    class FailOnce:
        async def aget_state(self, config):
            return await graph.aget_state(config)
        async def ainvoke(self, value, config):
            if fail_after_checkpoint:
                await graph.ainvoke(value, config)
            raise RuntimeError("lost delivery acknowledgment")
    worker = AgentWorker(platform, deps.store, deps, graph=FailOnce())
    asyncio.run(worker.run_once())
    # Rebuild both worker and graph from durable state.
    deps.checkpointer = SqliteSaver.from_conn_string(str(tmp_path / "checkpoints.sqlite3"))
    worker = AgentWorker(platform, deps.store, deps, graph=build_graph(deps))
    asyncio.run(worker.run_once())
    assert deps.store.get_case(cid)["status"] == "needs_review"
    assert deps.store.unconsumed_decisions(cid) == []
    assert pb.calls == 1
    assert len([e for e in deps.store.get_events(cid) if e["type"] == "decision" and "keep" in e["text"]]) == 1


def test_worker_replays_timeout_after_failed_delivery(tmp_path):
    deps, graph, cid, config, state, _ = setup_case(tmp_path, sqlite=True)
    asyncio.run(graph.ainvoke(state, config))
    deadline = deps.store.get_case(cid)["wait_until"]
    platform = PlatformStore(tmp_path / "platform.sqlite3"); platform.initialize()
    class BrokenDelivery:
        async def aget_state(self, cfg):
            return await graph.aget_state(cfg)
        async def ainvoke(self, *args):
            raise RuntimeError("worker unavailable")
    worker = AgentWorker(platform, deps.store, deps, graph=BrokenDelivery())
    asyncio.run(worker.run_once(now=deadline + 1))
    assert len(deps.store.unconsumed_decisions(cid)) == 1
    worker.graph = graph
    asyncio.run(worker.run_once(now=deadline + 2))
    assert deps.store.get_case(cid)["status"] == "needs_review"
    assert deps.store.unconsumed_decisions(cid) == []


def test_lost_retire_ack_is_not_delivered_to_next_approval(tmp_path):
    deps, graph, cid, config, state, pb = setup_case(tmp_path, sqlite=True)
    asyncio.run(graph.ainvoke(state, config))
    deps.store.record_decision(cid, key="lead_decision", choice="retire", employee_id="operator", source="api", now=time.time())
    platform = PlatformStore(tmp_path / "platform.sqlite3"); platform.initialize()
    class LostAck:
        async def aget_state(self, cfg):
            return await graph.aget_state(cfg)
        async def ainvoke(self, value, cfg):
            await graph.ainvoke(value, cfg)
            raise RuntimeError("acknowledgment lost")
    worker = AgentWorker(platform, deps.store, deps, graph=LostAck())
    asyncio.run(worker.run_once())
    worker.graph = graph
    asyncio.run(worker.run_once())
    assert deps.store.get_case(cid)["status"] == "waiting_approver"
    assert deps.store.get_case(cid)["addressed_to"] == "emp-casey-wong"
    assert deps.store.unconsumed_decisions(cid) == []
    assert graph.get_state(config).values["question"]["choices"] == ["approve", "reject"]
    assert pb.calls == 1


def test_approval_cannot_resolve_when_verification_fails(tmp_path):
    deps, graph, cid, config, state, pb = setup_case(tmp_path)
    deps.investigator = lambda *_: {"summary": "Needs approval", "confidence": "low"}
    async def unverifiable(*args, **kwargs):
        return False
    pb.verify = unverifiable
    asyncio.run(graph.ainvoke(state, config))
    asyncio.run(graph.ainvoke(Command(resume={"choice": "approve", "employee_id": "operator"}), config))
    assert pb.calls == 1
    assert deps.store.get_case(cid)["status"] == "needs_review"
    assert deps.store.get_actions(cid)[0]["verified_at"] is None


def test_wrong_employee_cannot_approve_retirement(tmp_path):
    deps, graph, cid, config, state, pb = setup_case(tmp_path)
    asyncio.run(graph.ainvoke(state, config))
    asyncio.run(graph.ainvoke(Command(resume={"choice": "retire", "employee_id": "emp-hannah-reed"}), config))
    asyncio.run(graph.ainvoke(Command(resume={"choice": "approve", "employee_id": "emp-hannah-reed"}), config))
    assert deps.store.get_case(cid)["status"] == "needs_review"
    assert len(deps.store.get_actions(cid)) == 1


def test_override_does_not_reopen_a_later_rejected_case_forever(tmp_path):
    deps, graph, cid, config, state, _ = setup_case(tmp_path)
    asyncio.run(graph.ainvoke(state, config))
    asyncio.run(graph.ainvoke(Command(resume={"choice": "undo", "employee_id": "operator"}), config))
    assert deps.store.open_findings_without_case("helio", open_finding_ids=["finding-aaa"]) == ["finding-aaa"]
    cid2 = deps.store.open_case("helio", "finding-aaa", route="investigating", now=time.time())
    cfg2 = {"configurable": {"thread_id": cid2}}
    asyncio.run(graph.ainvoke(state, cfg2))
    asyncio.run(graph.ainvoke(Command(resume={"choice": "reject", "employee_id": "operator"}), cfg2))
    assert deps.store.open_findings_without_case("helio", open_finding_ids=["finding-aaa"]) == []


def test_cached_resolution_before_dispatch_is_not_verification(tmp_path):
    from unittest.mock import Mock
    deps, graph, cid, config, state, pb = setup_case(tmp_path)
    platform = Mock()
    platform.finding.return_value = {"status": "resolved", "evidence_fresh": True, "last_verified": 1}
    deps.platform_store = platform
    deps.verify_timeout_seconds = 0.01
    asyncio.run(graph.ainvoke(state, config))
    assert deps.store.get_case(cid)["status"] == "needs_review"
    assert deps.store.get_actions(cid)[0]["verified_at"] is None


def test_worker_recovers_case_created_before_first_checkpoint(tmp_path):
    deps, graph, cid, config, state, pb = setup_case(tmp_path)
    platform = PlatformStore(deps.store.path); platform.initialize()
    deps.platform_store = platform
    now = time.time()
    with platform.connection() as db:
        db.execute("INSERT INTO findings VALUES (?, ?, 'inactive_owner_agent_access', 'high', 'open', 'current', ?, ?, ?, ?, NULL, ?, 'inactive_owner_active_access')",
                   ("finding-aaa", "helio", now, now, now, now, '{"agent_id":"agent:sales-research","employee_status":"inactive","classification":"confidential","record_count":6}'))
    worker = AgentWorker(platform, deps.store, deps, graph=graph)
    asyncio.run(worker.run_once())
    assert deps.store.get_case(cid)["status"] == "waiting_approver"
    assert pb.calls == 1


def test_target_is_rechecked_after_graph_verification(tmp_path):
    from unittest.mock import Mock
    deps, graph, cid, config, state, pb = setup_case(tmp_path)
    platform = Mock()
    def finding(*args):
        pb.status = "active"  # a competing change while the sync was running
        return {"status": "resolved", "evidence_fresh": True, "last_verified": time.time()}
    platform.finding.side_effect = finding
    deps.platform_store = platform
    deps.verify_timeout_seconds = 1
    asyncio.run(graph.ainvoke(state, config))
    assert deps.store.get_case(cid)["status"] == "needs_review"
    assert deps.store.get_actions(cid)[0]["verified_at"] is None


def test_upgrade_does_not_redispatch_legacy_pending_action(tmp_path):
    deps, graph, cid, config, state, pb = setup_case(tmp_path)
    aid = deps.store.record_action(cid, playbook="crm.pause_principal", target="agent:sales-research",
        idempotency_key=f"{cid}:crm.pause_principal:agent:sales-research", requested_by="awasi", now=time.time())
    # Recreate the original Phase 1 schema, whose pending rows did not record
    # whether a remote action had already been sent.
    with deps.store.connection() as db:
        columns = [row["name"] for row in db.execute("PRAGMA table_info(case_actions)")]
        if "dispatch_version" in columns:
            db.execute("ALTER TABLE case_actions DROP COLUMN dispatch_version")
    deps.store.initialize()
    asyncio.run(graph.ainvoke(state, config))
    assert pb.calls == 0
    assert deps.store.get_action(aid)["status"] == "unknown"
    assert deps.store.get_case(cid)["status"] == "needs_review"
