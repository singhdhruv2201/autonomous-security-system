"""LangGraph workflow skeleton for case lifecycle (investigate -> decide -> fix -> verify -> ask human -> report).

Nodes:
- gather: collects finding details and builds Facts
- investigate: generates assessment summary
- decide: computes Decision from Facts and confidence
- execute: creates action record and executes playbook
- verify: confirms system reached intended state
- notify_human: informs owner / approver via notifier
- wait_human: isolated interrupt() node waiting for decision
- process_human_decision: handles response or timeout
- report: finalizes and closes case
"""

import asyncio
from dataclasses import asdict, dataclass
import inspect
import time
from typing import Any, Callable, Dict, Optional, TypedDict

from langchain_core.runnables import RunnableConfig
from langgraph.graph import StateGraph, START, END
from langgraph.types import interrupt

from .cases import CaseStore
from .decision import Facts, Decision, decide


class WorkflowState(TypedDict, total=False):
    finding_id: str
    company_id: str
    facts: Dict[str, Any]
    investigation: Dict[str, Any]
    decision: Dict[str, Any]
    action_result: Dict[str, Any]
    human_input: Dict[str, Any]
    verified: bool


@dataclass
class WorkflowDeps:
    store: CaseStore
    checkpointer: Any
    notifier: Any
    investigator: Any
    playbooks: Dict[str, Any]
    clock: Callable[[], float] = time.time
    company_id: str = "helio"
    owner_deadline_seconds: int = 900
    platform_store: Any = None
    reader: Any = None
    crm_url: Optional[str] = None
    action_token: Optional[str] = None
    observer_token: Optional[str] = None
    verify_timeout_seconds: float = 0.0


def build_graph(deps: WorkflowDeps):
    builder = StateGraph(WorkflowState)

    def _get_case_id(state: WorkflowState, config: RunnableConfig) -> str:
        tid = config.get("configurable", {}).get("thread_id")
        if tid:
            return tid
        return state.get("finding_id", "unknown-case")

    async def gather(state: WorkflowState, config: RunnableConfig):
        case_id = _get_case_id(state, config)
        now = deps.clock()

        facts_dict = state.get("facts")
        if not facts_dict and deps.platform_store:
            company = state.get("company_id", deps.company_id)
            finding = deps.platform_store.finding(company, state.get("finding_id"))
            if finding:
                evidence = finding.get("evidence", {})
                facts_dict = {
                    "rule": finding.get("rule", "inactive_owner_agent_access"),
                    "target_kind": "agent",
                    "owner_status": evidence.get("employee_status", "inactive"),
                    "data_classification": evidence.get("classification", "confidential"),
                    "record_count": evidence.get("record_count", 0),
                    "exposure": "none",
                    "leaking_now": False,
                    "dependents": 0,
                    "human_override": False,
                    "has_lead": True,
                    "addressed_to": "emp-hannah-reed",
                    "agent_id": evidence.get("agent_id", "agent:sales-research"),
                    "grant_id": evidence.get("grant_id"),
                }

        if not facts_dict:
            facts_dict = {
                "rule": "inactive_owner_agent_access",
                "target_kind": "agent",
                "owner_status": "inactive",
                "data_classification": "confidential",
                "record_count": 6,
                "exposure": "none",
                "leaking_now": False,
                "dependents": 0,
                "human_override": False,
                "has_lead": True,
                "addressed_to": "emp-hannah-reed",
            }

        deps.store.add_event(
            case_id,
            key="gather",
            type="info",
            actor="awasi",
            text="Gathered evidence and facts for case",
            now=now,
        )
        return {"facts": facts_dict}

    async def investigate(state: WorkflowState, config: RunnableConfig):
        case_id = _get_case_id(state, config)
        now = deps.clock()
        facts_dict = state.get("facts", {})

        inv = deps.investigator(state.get("finding_id"), facts_dict)
        if inspect.iscoroutine(inv):
            inv = await inv

        summary = inv.get("summary", "")
        deps.store.update_case_status(case_id, status="investigating", summary=summary, now=now)
        deps.store.add_event(
            case_id,
            key="investigate",
            type="info",
            actor="awasi",
            text=summary,
            now=now,
        )
        return {"investigation": inv}

    async def decide_node(state: WorkflowState, config: RunnableConfig):
        case_id = _get_case_id(state, config)
        now = deps.clock()
        facts_dict = state.get("facts", {})
        inv = state.get("investigation", {})
        confidence = inv.get("confidence", "high")

        facts_obj = Facts(
            rule=facts_dict.get("rule", "inactive_owner_agent_access"),
            target_kind=facts_dict.get("target_kind", "agent"),
            owner_status=facts_dict.get("owner_status", "inactive"),
            data_classification=facts_dict.get("data_classification", "confidential"),
            record_count=facts_dict.get("record_count", 6),
            exposure=facts_dict.get("exposure", "none"),
            leaking_now=facts_dict.get("leaking_now", False),
            dependents=facts_dict.get("dependents", 0),
            human_override=facts_dict.get("human_override", False),
            has_lead=facts_dict.get("has_lead", True),
            addressed_to=facts_dict.get("addressed_to"),
            force_irreversible=facts_dict.get("force_irreversible", False),
        )

        d = decide(facts_obj, confidence=confidence)
        deps.store.add_event(
            case_id,
            key="decide",
            type="decision",
            actor="awasi",
            text=f"Decision: {d.action} ({d.reasoning})",
            now=now,
        )
        return {"decision": asdict(d)}

    def route_decision(state: WorkflowState) -> str:
        d = state.get("decision", {})
        action = d.get("action")
        if action in ("fix", "contain"):
            return "execute"
        elif action in ("ask_owner", "ask_approver"):
            return "notify_human"
        return "report"

    async def execute_node(state: WorkflowState, config: RunnableConfig):
        case_id = _get_case_id(state, config)
        now = deps.clock()
        d = state.get("decision", {})
        facts = state.get("facts", {})

        playbook_id = d.get("playbook")
        target = facts.get("agent_id") or "agent:sales-research"
        idempotency_key = f"{case_id}:{playbook_id}:{target}"

        # Check existing actions for idempotency
        actions = deps.store.get_actions(case_id)
        existing = next((a for a in actions if a["idempotency_key"] == idempotency_key), None)
        if existing and existing.get("status") == "executed":
            return {"action_result": existing.get("result", {})}

        action_id = deps.store.record_action(
            case_id,
            playbook=playbook_id,
            target=target,
            idempotency_key=idempotency_key,
            requested_by="awasi",
            now=now,
        )
        deps.store.update_case_status(case_id, status="fixing", now=now)

        pb = deps.playbooks.get(playbook_id)
        result = {}
        if pb and hasattr(pb, "run"):
            sig = inspect.signature(pb.run)
            kwargs: Dict[str, Any] = {}
            if "before_state" in sig.parameters:
                kwargs["before_state"] = None
            if "crm_url" in sig.parameters and deps.crm_url:
                kwargs["crm_url"] = deps.crm_url
            if "action_token" in sig.parameters and deps.action_token:
                kwargs["action_token"] = deps.action_token
            if "observer_token" in sig.parameters and deps.observer_token:
                kwargs["observer_token"] = deps.observer_token

            if inspect.iscoroutinefunction(pb.run):
                result = await pb.run(target, **kwargs)
            else:
                result = pb.run(target, **kwargs)

            before = result.get("before") if isinstance(result, dict) else None
            deps.store.update_action(action_id, status="executed", result=result, before=before, now=now)
            deps.store.add_event(
                case_id,
                key=f"execute:{playbook_id}",
                type="action",
                actor="awasi",
                text=f"Executed {playbook_id} on {target}",
                now=now,
            )
        return {"action_result": result}

    async def verify_node(state: WorkflowState, config: RunnableConfig):
        case_id = _get_case_id(state, config)
        now = deps.clock()
        d = state.get("decision", {})
        facts = state.get("facts", {})

        playbook_id = d.get("playbook")
        target = facts.get("agent_id") or "agent:sales-research"

        deps.store.update_case_status(case_id, status="verifying", now=now)

        pb = deps.playbooks.get(playbook_id)
        verified = True
        if pb and hasattr(pb, "verify"):
            sig = inspect.signature(pb.verify)
            kwargs: Dict[str, Any] = {}
            if "crm_url" in sig.parameters and deps.crm_url:
                kwargs["crm_url"] = deps.crm_url
            if "observer_token" in sig.parameters and deps.observer_token:
                kwargs["observer_token"] = deps.observer_token

            if inspect.iscoroutinefunction(pb.verify):
                verified = await pb.verify(target, **kwargs)
            else:
                verified = pb.verify(target, **kwargs)

        if verified and deps.platform_store:
            try:
                deps.platform_store.enqueue(deps.company_id, "awasi", "manual", deps.clock())
            except Exception:
                pass
            if deps.verify_timeout_seconds > 0:
                finding_id = state.get("finding_id")
                if finding_id:
                    resolved = False
                    start = deps.clock()
                    while deps.clock() - start < deps.verify_timeout_seconds:
                        f = deps.platform_store.finding(deps.company_id, finding_id)
                        if f and f.get("status") == "resolved":
                            resolved = True
                            break
                        await asyncio.sleep(0.2)
                    if not resolved:
                        verified = False

        if not verified:
            deps.store.close_case(case_id, status="needs_review", now=now)
            deps.store.add_event(
                case_id,
                key="verify",
                type="error",
                actor="awasi",
                text=f"Verification failed for {playbook_id}",
                now=now,
            )
            return {"verified": False}

        deps.store.add_event(
            case_id,
            key="verify",
            type="verification",
            actor="awasi",
            text=f"Verified {playbook_id} on {target}",
            now=now,
        )
        return {"verified": True}

    def route_after_verify(state: WorkflowState) -> str:
        if not state.get("verified", True):
            return END
        d = state.get("decision", {})
        followup = d.get("followup")
        if followup in ("ask_lead", "ask_approver", "tell_owner"):
            return "notify_human"
        return "report"

    async def notify_human(state: WorkflowState, config: RunnableConfig):
        case_id = _get_case_id(state, config)
        now = deps.clock()
        d = state.get("decision", {})
        facts = state.get("facts", {})

        followup = d.get("followup")
        action = d.get("action")
        if followup == "ask_lead":
            status = "waiting_approver"
            addressed_to = facts.get("addressed_to", "emp-hannah-reed")
        elif followup == "ask_approver" or action == "ask_approver":
            status = "waiting_approver"
            addressed_to = "emp-casey-wong"
        else:
            status = "waiting_owner"
            addressed_to = facts.get("addressed_to", "emp-maya-chen")

        deadline = now + deps.owner_deadline_seconds
        deps.store.update_case_status(
            case_id,
            status=status,
            addressed_to=addressed_to,
            wait_until=deadline,
            now=now,
        )
        deps.store.add_event(
            case_id,
            key="notify_human",
            type="notification",
            actor="awasi",
            text=f"Awaiting decision from {addressed_to}",
            now=now,
        )

        if hasattr(deps.notifier, "notify"):
            payload = {"addressed_to": addressed_to, "deadline": deadline, "decision": d}
            if inspect.iscoroutinefunction(deps.notifier.notify):
                await deps.notifier.notify(case_id, "question", payload)
            else:
                deps.notifier.notify(case_id, "question", payload)

        return {"status": status}

    def wait_human(state: WorkflowState, config: RunnableConfig):
        case_id = _get_case_id(state, config)
        ans = interrupt({"case_id": case_id, "prompt": "decision_required"})
        return {"human_input": ans}

    async def process_human_decision(state: WorkflowState, config: RunnableConfig):
        case_id = _get_case_id(state, config)
        now = deps.clock()
        human_input = state.get("human_input", {})
        choice = human_input.get("choice", "retire")
        employee_id = human_input.get("employee_id", "operator")

        deps.store.add_event(
            case_id,
            key=f"human_decision:{choice}",
            type="decision",
            actor=employee_id,
            text=f"Decision recorded: {choice} by {employee_id}",
            now=now,
        )

        if choice == "undo":
            d = state.get("decision", {})
            playbook_id = d.get("playbook")
            pb = deps.playbooks.get(playbook_id)
            if pb and hasattr(pb, "undo"):
                target = state.get("facts", {}).get("agent_id", "agent:sales-research")
                actions = deps.store.get_actions(case_id)
                action = next((a for a in actions if a["playbook"] == playbook_id), None)
                before = action.get("before", {}) if action else {}
                if inspect.iscoroutinefunction(pb.undo):
                    await pb.undo(target, before=before)
                else:
                    pb.undo(target, before=before)
            deps.store.close_case(case_id, status="resolved", now=now)
        elif choice == "keep":
            deps.store.close_case(case_id, status="needs_review", now=now)
        else:
            deps.store.close_case(case_id, status="resolved", now=now)
        return {}

    async def report(state: WorkflowState, config: RunnableConfig):
        case_id = _get_case_id(state, config)
        now = deps.clock()
        case = deps.store.get_case(case_id)
        if case and case["status"] not in ("resolved", "needs_review", "dismissed"):
            deps.store.close_case(case_id, status="resolved", now=now)

        deps.store.add_event(
            case_id,
            key="report",
            type="info",
            actor="awasi",
            text="Case workflow finished",
            now=now,
        )
        return {}

    builder.add_node("gather", gather)
    builder.add_node("investigate", investigate)
    builder.add_node("decide", decide_node)
    builder.add_node("execute", execute_node)
    builder.add_node("verify", verify_node)
    builder.add_node("notify_human", notify_human)
    builder.add_node("wait_human", wait_human)
    builder.add_node("process_human_decision", process_human_decision)
    builder.add_node("report", report)

    builder.add_edge(START, "gather")
    builder.add_edge("gather", "investigate")
    builder.add_edge("investigate", "decide")
    builder.add_conditional_edges(
        "decide",
        route_decision,
        {"execute": "execute", "notify_human": "notify_human", "report": "report"},
    )
    builder.add_edge("execute", "verify")
    builder.add_conditional_edges(
        "verify",
        route_after_verify,
        {"notify_human": "notify_human", "report": "report", END: END},
    )
    builder.add_edge("notify_human", "wait_human")
    builder.add_edge("wait_human", "process_human_decision")
    builder.add_edge("process_human_decision", "report")
    builder.add_edge("report", END)

    # Unwrap checkpointer if it is a context manager
    checkpointer = deps.checkpointer
    if hasattr(checkpointer, "__enter__") and not hasattr(checkpointer, "get_tuple"):
        checkpointer = checkpointer.__enter__()
    if hasattr(checkpointer, "setup"):
        checkpointer.setup()

    # Bridge synchronous checkpointers (like SqliteSaver) for async invocations
    if hasattr(checkpointer, "get_tuple"):
        async def _aget_tuple(config):
            return checkpointer.get_tuple(config)

        async def _aput(config, checkpoint, metadata, new_versions):
            return checkpointer.put(config, checkpoint, metadata, new_versions)

        async def _aput_writes(config, writes, task_id, task_path=""):
            return checkpointer.put_writes(config, writes, task_id, task_path)

        async def _alist(config, **kwargs):
            for item in checkpointer.list(config, **kwargs):
                yield item

        checkpointer.aget_tuple = _aget_tuple
        checkpointer.aput = _aput
        checkpointer.aput_writes = _aput_writes
        checkpointer.alist = _alist

    return builder.compile(checkpointer=checkpointer)
