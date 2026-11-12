"""Durable dispatch boundary shared by execution and undo.

A dispatched action is never sent again. Recovery only reads the target, and
an indeterminate outcome requires review. Original state survives process loss.
"""
import inspect

from .playbooks import PlaybookError, UnknownOutcome
from .tracing import Tracer


async def call_playbook(method, target, deps, **kwargs):
    values = {"crm_url": deps.crm_url, "action_token": deps.action_token,
              "observer_token": deps.observer_token, **kwargs}
    parameters = inspect.signature(method).parameters
    arguments = {key: value for key, value in values.items() if key in parameters}
    result = method(target, **arguments)
    return await result if inspect.isawaitable(result) else result


async def dispatch(deps, case_id, playbook_id, target, *, requested_by="awasi", undo_of=None):
    store = deps.store
    key = f"{case_id}:{playbook_id}:{target}" if not undo_of else f"undo:{undo_of['id']}"
    action_id = store.record_action(case_id, playbook=playbook_id, target=target,
        idempotency_key=key, requested_by=requested_by, now=deps.clock(),
        undo_of=undo_of["id"] if undo_of else None)
    action = store.get_action(action_id)
    if action["status"] != "pending":
        return action
    if action["dispatch_version"] < 2:
        # Older pending rows cannot distinguish an unsent request from a lost
        # result. Quarantine them on upgrade rather than assuming safe retry.
        store.update_action(action_id, status="unknown", now=deps.clock(),
                            result={"reason": "legacy_dispatch_state_unknown"})
        return store.get_action(action_id)
    pb = deps.playbooks.get(playbook_id)
    expected = {"expected_state": undo_of["before"]["principal_status"]} if undo_of else {}
    tracer = Tracer(store, case_id) if hasattr(store, "record_span") else None

    async def reconcile():
        try:
            if pb:
                if tracer:
                    with tracer.span("action", f"{playbook_id}.verify", attributes={"target": target, "playbook": playbook_id}):
                        return bool(await call_playbook(pb.verify, target, deps, **expected))
                return bool(await call_playbook(pb.verify, target, deps, **expected))
            return False
        except Exception:
            return False

    if action["dispatched_at"] is not None:
        # Includes old pending rows without a before-state: never repeat dispatch.
        ok = action["before"] is not None and await reconcile()
        store.update_action(action_id, status="executed" if ok else "unknown", now=deps.clock(),
                            result={"reconciled": bool(ok)})
        return store.get_action(action_id)

    try:
        if not pb or not hasattr(pb, "read"):
            raise PlaybookError("Playbook does not support durable execution")
        before = action["before"]
        if before is None:
            if tracer:
                with tracer.span("action", f"{playbook_id}.read", attributes={"target": target, "playbook": playbook_id}):
                    before = await call_playbook(pb.read, target, deps)
            else:
                before = await call_playbook(pb.read, target, deps)
        if not isinstance(before, dict) or not before:
            raise PlaybookError("Missing original target state")
    except Exception:
        store.update_action(action_id, status="failed", now=deps.clock(), result={"reason": "preparation_failed"})
        return store.get_action(action_id)

    # This commit MUST precede the network call. A crash after it is ambiguous,
    # even if the request was never sent; prefer review over a second mutation.
    store.update_action(action_id, status="pending", before=before,
                        dispatched_at=deps.clock(), now=deps.clock())
    try:
        if undo_of:
            op_name = f"{playbook_id}.undo"
            if tracer:
                with tracer.span("action", op_name, attributes={"target": target, "playbook": playbook_id}):
                    result = await call_playbook(pb.undo, target, deps, before=undo_of["before"], before_state=before)
            else:
                result = await call_playbook(pb.undo, target, deps, before=undo_of["before"], before_state=before)
        else:
            op_name = f"{playbook_id}.run"
            if tracer:
                with tracer.span("action", op_name, attributes={"target": target, "playbook": playbook_id}):
                    result = await call_playbook(pb.run, target, deps, before_state=before)
            else:
                result = await call_playbook(pb.run, target, deps, before_state=before)
        status = "executed"
    except UnknownOutcome:
        status = "executed" if await reconcile() else "unknown"
        result = {"reconciled": status == "executed"}
    except PlaybookError:
        status = "failed"
        result = {"reason": "action_rejected"}
    except Exception:
        status = "executed" if await reconcile() else "unknown"
        result = {"reconciled": status == "executed"}
    # Do not catch persistence failures as action errors. Recovery uses the
    # dispatch marker to reconcile and the original before-state stays intact.
    store.update_action(action_id, status=status, result=result, now=deps.clock())
    return store.get_action(action_id)
