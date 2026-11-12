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
from .decision import Facts, decide
from .actions import call_playbook, dispatch
from . import workbench_workflow
from .workbench_workflow import facts_from_finding


class WorkflowState(TypedDict, total=False):
    finding_id: str
    company_id: str
    facts: Dict[str, Any]
    investigation: Dict[str, Any]
    decision: Dict[str, Any]
    action_result: Dict[str, Any]
    human_input: Dict[str, Any]
    verified: bool
    action_id: str
    question: Dict[str, Any]
    next_step: str
    processed_decisions: list[str]


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
    workbench_url: Optional[str] = None
    workbench_observer_token: Optional[str] = None
    workbench_action_token: Optional[str] = None


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
                facts_dict = facts_from_finding(finding)

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

        facts_dict = dict(facts_dict)
        facts_dict["human_override"] = bool(facts_dict.get("human_override")) or deps.store.has_human_override(
            state.get("company_id", deps.company_id), state["finding_id"])
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

        inv_fn = getattr(deps.investigator, "investigate", deps.investigator)
        try:
            inv = inv_fn(state.get("finding_id"), facts_dict, case_id=case_id)
        except TypeError:
            inv = inv_fn(state.get("finding_id"), facts_dict)
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

    def action_target(state):
        if workbench_workflow.is_workbench(state):
            return workbench_workflow.target_for(state)
        facts = state.get("facts", {})
        if state.get("decision", {}).get("playbook") == "crm.revoke_grant":
            return facts.get("grant_id")
        return facts.get("agent_id")

    async def execute_node(state: WorkflowState, config: RunnableConfig):
        case_id = _get_case_id(state, config)
        playbook_id = state.get("decision", {}).get("playbook")
        target = action_target(state)
        if not target or not playbook_id:
            deps.store.close_case(case_id, status="needs_review", now=deps.clock())
            return {"action_result": {"status": "failed"}, "verified": False}
        deps.store.update_case_status(case_id, status="fixing", now=deps.clock())
        action = await dispatch(deps, case_id, playbook_id, target,
                                requested_by=state.get("human_input", {}).get("employee_id", "awasi"))
        deps.store.add_event(case_id, key=f"execute:{action['id']}", type="action", actor="awasi",
            text=f"Action {playbook_id} on {target}: {action['status']}", now=deps.clock())
        if action["status"] != "executed":
            deps.store.close_case(case_id, status="needs_review", now=deps.clock())
        return {"action_id": action["id"], "action_result": {"status": action["status"]}, "verified": False}

    async def verify_node(state: WorkflowState, config: RunnableConfig):
        case_id = _get_case_id(state, config)
        now = deps.clock()
        d = state.get("decision", {})
        playbook_id = d.get("playbook")
        target = action_target(state)

        deps.store.update_case_status(case_id, status="verifying", now=now)

        pb = deps.playbooks.get(playbook_id)
        verified = False
        try:
            if pb and state.get("action_result", {}).get("status") == "executed":
                verified = await call_playbook(pb.verify, target, deps)
        except Exception:
            verified = False

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
                        if (f and f.get("status") == "resolved" and f.get("evidence_fresh")
                                and f.get("last_verified", 0) >= deps.store.get_action(state["action_id"])["dispatched_at"]):
                            resolved = True
                            break
                        await asyncio.sleep(0.2)
                    if not resolved:
                        verified = False

        if verified:
            # The target may have changed while waiting for observation to catch
            # up. Check it again before recording a verified outcome.
            try:
                verified = await call_playbook(pb.verify, target, deps)
            except Exception:
                verified = False

        if not verified:
            deps.store.close_case(case_id, status="needs_review", now=now)
            deps.store.add_event(
                case_id,
                key=f"verify:{state.get('action_id')}",
                type="error",
                actor="awasi",
                text=f"Verification failed for {playbook_id}",
                now=now,
            )
            return {"verified": False}

        deps.store.add_event(
            case_id,
            key=f"verify:{state.get('action_id')}",
            type="verification",
            actor="awasi",
            text=f"Verified {playbook_id} on {target}",
            now=now,
        )
        deps.store.update_action(state["action_id"], status="executed", now=deps.clock(), verified_at=deps.clock())
        return {"verified": True}

    def route_after_verify(state: WorkflowState) -> str:
        if not state.get("verified", True):
            return "report"
        d = state.get("decision", {})
        followup = d.get("followup")
        if followup in ("ask_lead", "ask_approver", "tell_owner"):
            return "notify_human"
        return "report"

    async def notify_human(state: WorkflowState, config: RunnableConfig):
        case_id = _get_case_id(state, config)
        if workbench_workflow.is_workbench(state):
            return await workbench_workflow.notify(deps, case_id, state)
        d = state.get("decision", {})
        facts = state.get("facts", {})
        followup = d.get("followup")
        if followup == "ask_lead":
            role, addressed_to = "lead", facts.get("addressed_to", "emp-hannah-reed")
            choices = ["retire", "keep", "undo"]
            key = "lead_decision"
        elif followup == "ask_approver" or d.get("action") == "ask_approver":
            role, addressed_to = "approver", "emp-casey-wong"
            choices = ["approve", "reject"]
            key = "approve:" + str(d.get("playbook"))
        else:
            role, addressed_to = "owner", facts.get("addressed_to")
            choices = ["keep", "reject"]
            key = "owner_decision"
        question = {"key": key, "role": role, "choices": choices, "addressed_to": addressed_to}
        # Preserve the first deadline on node replay, rather than extending it.
        event = next((e for e in deps.store.get_events(case_id) if e["key"] == f"question:{key}"), None)
        deadline = event["data"]["deadline"] if event else deps.clock() + deps.owner_deadline_seconds
        question_payload = {**question, "deadline": deadline, "facts": facts}
        deps.store.offer_question(case_id, question_payload, now=deps.clock())
        deps.store.add_event(case_id, key=f"question:{key}", type="notification", actor="awasi",
            text=f"Awaiting decision from {addressed_to}", data={**question, "deadline": deadline}, now=deps.clock())
        if hasattr(deps.notifier, "notify"):
            payload = {**question_payload, "decision": d}
            result = deps.notifier.notify(case_id, key, payload)
            if inspect.isawaitable(result):
                await result
        return {"question": question_payload}

    def wait_human(state: WorkflowState, config: RunnableConfig):
        ans = interrupt({"case_id": _get_case_id(state, config), **state.get("question", {})})
        return {"human_input": ans}

    async def process_human_decision(state: WorkflowState, config: RunnableConfig):
        case_id = _get_case_id(state, config)
        answer = state.get("human_input", {})
        choice = answer.get("choice")
        employee = answer.get("employee_id")
        question = state.get("question", {})
        processed = list(state.get("processed_decisions", []))
        if answer.get("id") and answer["id"] not in processed:
            processed.append(answer["id"])
        update = {"processed_decisions": processed, "next_step": "report"}
        deps.store.add_event(case_id, key=f"human_decision:{answer.get('id') or question.get('key')}:{choice}",
            type="decision", actor=employee or "operator", text=f"Decision recorded: {choice} by {employee}", now=deps.clock())
        if workbench_workflow.is_workbench(state):
            return await workbench_workflow.process(deps, case_id, state, update)
        authorized = employee in ("operator", question.get("addressed_to"))
        valid_key = answer.get("key", question.get("key")) == question.get("key")
        if not authorized or not valid_key or choice not in question.get("choices", []):
            deps.store.close_case(case_id, status="needs_review", now=deps.clock())
            return update
        d = dict(state.get("decision", {}))
        if choice == "retire" and question.get("role") == "lead":
            d.update(action="ask_approver", playbook="crm.revoke_grant", followup=None)
            return {**update, "decision": d, "next_step": "notify_human", "verified": False}
        if choice == "approve" and question.get("role") == "approver" and d.get("playbook"):
            d.update(action="fix", followup=None)
            return {**update, "decision": d, "next_step": "execute", "verified": False}
        if choice == "undo":
            # Undo is itself a durable action. Set the override before dispatch so
            # an uncertain outcome can never trigger automatic containment again.
            actions = deps.store.get_actions(case_id)
            original = next((a for a in actions if a["playbook"] == "crm.pause_principal"
                             and not a["undo_of"] and a["status"] in ("executed", "undone")), None)
            if original and original["before"]:
                deps.store.set_human_override(case_id, now=deps.clock())
                undo = await dispatch(deps, case_id, original["playbook"], original["target"],
                                      requested_by=employee, undo_of=original)
                try:
                    ok = undo["status"] == "executed" and await call_playbook(
                        deps.playbooks[original["playbook"]].verify, original["target"], deps,
                        expected_state=original["before"]["principal_status"])
                except Exception:
                    ok = False
                if ok:
                    deps.store.update_action(undo["id"], status="executed", verified_at=deps.clock(), now=deps.clock())
                    deps.store.update_action(original["id"], status="undone", now=deps.clock())
                deps.store.add_event(case_id, key=f"undo:{original['id']}", type="action", actor=employee,
                    text="Undo verified" if ok else "Undo requires review", now=deps.clock())
        # Keep, reject, invalid answers, timeouts and undo leave the security
        # question unresolved. Only execute -> verify can resolve a case.
        deps.store.close_case(case_id, status="needs_review", now=deps.clock())
        return update

    async def report(state: WorkflowState, config: RunnableConfig):
        case_id = _get_case_id(state, config)
        now = deps.clock()
        case = deps.store.get_case(case_id)
        if case and case["status"] not in ("resolved", "needs_review", "dismissed"):
            deps.store.close_case(case_id, status="resolved" if state.get("verified") else "needs_review", now=now)

        deps.store.add_event(
            case_id,
            key="report",
            type="info",
            actor="awasi",
            text="Case workflow finished",
            now=now,
        )
        if hasattr(deps.notifier, "notify"):
            final_case = deps.store.get_case(case_id)
            payload = {"status": final_case["status"] if final_case else "resolved", "decision": state.get("decision", {}), "facts": state.get("facts", {}), "human_choice": state.get("human_input", {}).get("choice")}
            result = deps.notifier.notify(case_id, "outcome", payload)
            if inspect.isawaitable(result):
                await result
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
    builder.add_conditional_edges(
        "execute",
        lambda state: "verify" if state.get("action_result", {}).get("status") == "executed" else "report",
        {"verify": "verify", "report": "report"},
    )
    builder.add_conditional_edges(
        "verify",
        route_after_verify,
        {"notify_human": "notify_human", "report": "report"},
    )
    builder.add_edge("notify_human", "wait_human")
    builder.add_edge("wait_human", "process_human_decision")
    builder.add_conditional_edges("process_human_decision", lambda state: state["next_step"],
                                  {"report": "report", "execute": "execute", "notify_human": "notify_human"})
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
