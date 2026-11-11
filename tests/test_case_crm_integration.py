"""Workflow integration with real HTTP, CRM SQLite and durable checkpoints."""
import asyncio
import time

import httpx
import pytest
from fastapi.responses import JSONResponse
from langgraph.types import Command
from langgraph.checkpoint.sqlite import SqliteSaver

from helio_crm.api import create_app
from helio_security.playbooks import REGISTRY
from helio_security.workflow import build_graph
from test_graph import serve
from test_playbooks_crm import make_tokens, EMPLOYEES
from test_case_recovery import setup_case, Crash


@pytest.fixture
def environment(tmp_path):
    tokens = make_tokens()
    app = create_app(tmp_path / "crm.sqlite3", tokens, lambda: EMPLOYEES)
    fault = {"mode": None, "patches": 0}
    @app.middleware("http")
    async def faults(request, call_next):
        if request.method == "PATCH":
            fault["patches"] += 1
            if fault["mode"] == "before_commit":
                return JSONResponse({}, status_code=503)
            response = await call_next(request)
            if fault["mode"] in ("after_commit", "unreadable"):
                return JSONResponse({}, status_code=503)
            return response
        if fault["mode"] == "unreadable" and fault["patches"] and request.url.path == "/accounts":
            return JSONResponse({}, status_code=503)
        return await call_next(request)
    with serve(app) as url:
        deps, graph, cid, cfg, state, _ = setup_case(tmp_path, sqlite=True)
        deps.crm_url = url; deps.action_token = tokens["actions"]; deps.observer_token = tokens["observer"]
        deps.playbooks = REGISTRY
        yield deps, graph, cid, cfg, state, fault


@pytest.mark.parametrize("mode,expected", [("before_commit", "unknown"), ("after_commit", "executed"), ("unreadable", "unknown")])
def test_lost_crm_response_reconciles_or_stops_without_retry(environment, mode, expected):
    deps, graph, cid, cfg, state, fault = environment
    fault["mode"] = mode
    asyncio.run(graph.ainvoke(state, cfg))
    asyncio.run(graph.ainvoke(None, cfg))
    assert fault["patches"] == 1
    action = deps.store.get_actions(cid)[0]
    assert action["status"] == expected
    assert action["before"]["principal_status"] == "active"
    if expected == "unknown":
        assert deps.store.get_case(cid)["status"] == "needs_review"


@pytest.mark.parametrize("lose_undo_response", [False, True])
def test_real_workflow_undo_restores_state_and_records_override(environment, lose_undo_response):
    deps, graph, cid, cfg, state, fault = environment
    asyncio.run(graph.ainvoke(state, cfg))
    if lose_undo_response:
        fault["mode"] = "after_commit"
    asyncio.run(graph.ainvoke(Command(resume={"choice": "undo", "employee_id": "operator"}), cfg))
    actions = deps.store.get_actions(cid)
    assert actions[0]["status"] == "undone"
    assert actions[1]["verified_at"] is not None
    assert deps.store.has_human_override("helio", "finding-aaa")
    response = httpx.get(deps.crm_url + "/accounts", headers={"Authorization": "Bearer " + deps.observer_token})
    account = next(a for a in response.json() if a["id"] == "agent:sales-research")
    assert account["principal_status"] == "active"
    # No credentials may land in the durable case detail.
    detail = str(deps.store.get_case_full(cid))
    assert deps.action_token not in detail and deps.observer_token not in detail


def test_real_retirement_requires_approval_then_revokes_correct_grant(environment):
    deps, graph, cid, cfg, state, _ = environment
    asyncio.run(graph.ainvoke(state, cfg))
    asyncio.run(graph.ainvoke(Command(resume={"choice": "retire", "employee_id": "emp-hannah-reed"}), cfg))
    assert deps.store.get_case(cid)["status"] == "waiting_approver"
    assert len(deps.store.get_actions(cid)) == 1
    asyncio.run(graph.ainvoke(Command(resume={"choice": "approve", "employee_id": "emp-casey-wong"}), cfg))
    assert deps.store.get_case(cid)["status"] == "resolved"
    action = deps.store.get_actions(cid)[1]
    assert action["target"] == "grant-sales-agent" and action["verified_at"] is not None
    response = httpx.get(deps.crm_url + "/grants", headers={"Authorization": "Bearer " + deps.observer_token})
    grants = {g["id"]: g["status"] for g in response.json()}
    assert grants["grant-sales-agent"] == "revoked" and grants["grant-maya"] == "active"


def test_real_crm_crash_after_commit_preserves_before_without_second_patch(environment, monkeypatch):
    deps, graph, cid, cfg, state, fault = environment
    original = deps.store.update_action
    def crash_after_commit(*args, **kwargs):
        if kwargs.get("status") == "executed":
            raise Crash()
        return original(*args, **kwargs)
    monkeypatch.setattr(deps.store, "update_action", crash_after_commit)
    with pytest.raises(Crash):
        asyncio.run(graph.ainvoke(state, cfg))
    monkeypatch.setattr(deps.store, "update_action", original)
    deps.checkpointer = SqliteSaver.from_conn_string(str(deps.store.path.parent / "checkpoints.sqlite3"))
    asyncio.run(build_graph(deps).ainvoke(None, cfg))
    assert fault["patches"] == 1
    assert deps.store.get_actions(cid)[0]["before"]["principal_status"] == "active"


def test_api_decisions_drive_worker_and_real_crm(environment, tmp_path):
    from unittest.mock import Mock
    from fastapi.testclient import TestClient
    from helio_security.api import create_app as security_app
    from helio_security.settings import Settings
    from helio_security.platform_store import PlatformStore
    from helio_security.agent_worker import AgentWorker
    import secrets

    deps, graph, cid, cfg, state, _ = environment
    asyncio.run(graph.ainvoke(state, cfg))
    settings = Settings(operator_token=secrets.token_urlsafe(32), viewer_token=secrets.token_urlsafe(32),
                        observer_token=deps.observer_token, db_path=deps.store.path, sync_interval=0)
    platform = PlatformStore(deps.store.path)
    worker = AgentWorker(platform, deps.store, deps, graph=graph)
    with TestClient(security_app(settings, graph=Mock(company_id="helio"), reader=Mock(), store=platform, start_worker=False)) as client:
        headers = {"Authorization": "Bearer " + settings.operator_token}
        path = f"/cases/{cid}"
        for key, choice in (("lead_decision", "retire"), ("approve:crm.revoke_grant", "approve")):
            assert client.post(path + "/decisions", json={"key": key, "choice": choice}, headers=headers).status_code == 200
            asyncio.run(worker.run_once())
        detail = client.get(path, headers=headers).json()
        assert detail["status"] == "resolved"
        assert [a["target"] for a in detail["actions"]] == ["agent:sales-research", "grant-sales-agent"]
        assert all(a["verified_at"] for a in detail["actions"])
        assert deps.store.unconsumed_decisions(cid) == []


def test_crm_version_change_after_preparation_is_not_overwritten(environment, monkeypatch):
    deps, graph, cid, cfg, state, fault = environment
    playbook = deps.playbooks["crm.pause_principal"]
    original = playbook.read
    async def concurrent_change(target, crm_url, observer_token):
        before = await original(target, crm_url, observer_token)
        async with httpx.AsyncClient() as client:
            response = await client.patch(crm_url + f"/principals/{target}/access",
                headers={"Authorization": "Bearer " + deps.action_token},
                json={"expected_version": before["access_version"], "principal_status": "disabled"})
            assert response.status_code == 200
        return before
    monkeypatch.setattr(playbook, "read", concurrent_change)
    asyncio.run(graph.ainvoke(state, cfg))
    assert deps.store.get_case(cid)["status"] == "needs_review"
    assert deps.store.get_actions(cid)[0]["status"] == "failed"
    assert fault["patches"] == 2  # competing update plus rejected stale request


def test_real_undo_crash_reconciles_without_second_undo(environment, monkeypatch):
    deps, graph, cid, cfg, state, fault = environment
    asyncio.run(graph.ainvoke(state, cfg))
    original = deps.store.update_action
    def crash_on_undo_result(action_id, **kwargs):
        if kwargs.get("status") == "executed" and deps.store.get_action(action_id)["undo_of"]:
            raise Crash()
        return original(action_id, **kwargs)
    monkeypatch.setattr(deps.store, "update_action", crash_on_undo_result)
    with pytest.raises(Crash):
        asyncio.run(graph.ainvoke(Command(resume={"choice": "undo", "employee_id": "operator"}), cfg))
    monkeypatch.setattr(deps.store, "update_action", original)
    deps.checkpointer = SqliteSaver.from_conn_string(str(deps.store.path.parent / "checkpoints.sqlite3"))
    asyncio.run(build_graph(deps).ainvoke(None, cfg))
    assert fault["patches"] == 2
    assert deps.store.get_actions(cid)[0]["status"] == "undone"


def test_timeout_after_real_crm_commit_is_reconciled(environment, monkeypatch):
    deps, graph, cid, cfg, state, fault = environment
    original = httpx.AsyncClient.patch
    async def lose_response(client, *args, **kwargs):
        await original(client, *args, **kwargs)
        raise httpx.ReadTimeout("simulated lost response")
    monkeypatch.setattr(httpx.AsyncClient, "patch", lose_response)
    asyncio.run(graph.ainvoke(state, cfg))
    assert fault["patches"] == 1
    assert deps.store.get_actions(cid)[0]["status"] == "executed"
    assert deps.store.get_actions(cid)[0]["verified_at"] is not None
