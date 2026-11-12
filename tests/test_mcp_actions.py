"""Tests for action MCP server, stdio lifecycle, and durable playbook integration."""
import asyncio
from pathlib import Path
import pytest
from unittest.mock import patch, MagicMock

from helio_security.mcp_actions import (
    actions_server,
    create_mcp_playbooks,
    crm_pause_principal,
    crm_revoke_grant,
    crm_undo_pause,
    crm_read,
    crm_verify,
)
from helio_security.playbooks import PlaybookError


def test_action_tools_have_destructive_markers():
    async def scenario():
        raw_tools = await actions_server.list_tools()
        tools_by_name = {t.name: t for t in raw_tools}
        assert "crm_pause_principal" in tools_by_name
        assert "crm_revoke_grant" in tools_by_name
        assert "crm_undo_pause" in tools_by_name
        assert "crm_read" in tools_by_name
        assert "crm_verify" in tools_by_name

        # Mutation tools must be marked destructive
        for name in ("crm_pause_principal", "crm_revoke_grant", "crm_undo_pause"):
            t = tools_by_name[name]
            assert "destructive" in getattr(t, "tags", []) or "destructive" in str(getattr(t, "description", "")).lower()

        # Read tools must not be marked destructive
        assert "destructive" not in getattr(tools_by_name["crm_read"], "tags", [])
        assert "destructive" not in getattr(tools_by_name["crm_verify"], "tags", [])
    asyncio.run(scenario())


def test_mcp_playbooks_delegation():
    async def scenario():
        from langchain.mcp import MCPAdapter
        adapter = MCPAdapter(actions_server)
        playbooks = create_mcp_playbooks(adapter)
        assert "crm.pause_principal" in playbooks
        assert "crm.revoke_grant" in playbooks
        assert playbooks["crm.pause_principal"].reversible is True
        assert playbooks["crm.revoke_grant"].reversible is False
    asyncio.run(scenario())


def test_non_agent_target_refusal():
    async def scenario():
        # Attempting to read or pause an account that is not an agent raises PlaybookError
        fake_acc = {"id": "user:bob", "kind": "employee", "principal_status": "active", "access_version": 1}
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = [fake_acc]

        with patch("httpx.AsyncClient.get", return_value=mock_resp):
            with pytest.raises(Exception, match="must be an agent|does not belong to an agent"):
                await crm_read("crm.pause_principal", "user:bob")
    asyncio.run(scenario())


def test_stdio_smoke_and_clean_shutdown():
    async def scenario():
        from langchain.mcp import MCPAdapter
        server_path = Path(__file__).parent.parent / "src" / "helio_security" / "mcp_actions.py"
        assert server_path.exists()
        async with MCPAdapter(server_path) as adapter:
            tools = await adapter.list_tools()
            names = {t.name for t in tools}
            assert "crm_pause_principal" in names
            assert "crm_revoke_grant" in names
            assert "crm_read" in names
            playbooks = create_mcp_playbooks(adapter)
            with pytest.raises(PlaybookError):
                await playbooks["crm.pause_principal"].read("user:bob")
    asyncio.run(scenario())


class FakePlaybook:
    async def read(self, *args, **kwargs):
        return {"principal_status": "active", "access_version": 1}

    async def verify(self, *args, **kwargs):
        return False

    async def run(self, *args, **kwargs):
        return {"status": "executed"}


def test_worker_wired_mcp_dispatch_can_execute(tmp_path):
    from types import SimpleNamespace
    import time
    from helio_security.cases import CaseStore
    from helio_security.actions import dispatch

    store = CaseStore(tmp_path / "case.sqlite3")
    store.initialize()
    cid = store.open_case("helio", "finding-1", route="investigating", now=time.time())

    deps = SimpleNamespace(
        store=store,
        clock=time.time,
        playbooks=create_mcp_playbooks(actions_server),
        crm_url="http://localhost:1",
        action_token="fake",
        observer_token="fake",
    )
    with patch.dict("helio_security.mcp_actions.REGISTRY", {"crm.pause_principal": FakePlaybook()}):
        result = asyncio.run(dispatch(deps, cid, "crm.pause_principal", "agent:sales-research"))
    assert result["status"] == "executed", result["result"]


def test_mcp_false_verification_remains_false():
    pb = create_mcp_playbooks(actions_server)["crm.pause_principal"]
    with patch.dict("helio_security.mcp_actions.REGISTRY", {"crm.pause_principal": FakePlaybook()}):
        result = asyncio.run(pb.verify("agent:sales-research"))
    assert result is False


def test_mcp_action_tool_parameters_exclude_credentials_and_urls():
    async def scenario():
        raw_tools = await actions_server.list_tools()
        tools_by_name = {t.name: t for t in raw_tools}
        for name in ("crm_pause_principal", "crm_revoke_grant", "crm_undo_pause", "crm_read", "crm_verify"):
            t = tools_by_name[name]
            schema = t.parameters
            props = schema.get("properties", {})
            assert "crm_url" not in props, f"{name} exposed crm_url in tool parameters"
            assert "action_token" not in props, f"{name} exposed action_token in tool parameters"
            assert "observer_token" not in props, f"{name} exposed observer_token in tool parameters"
    asyncio.run(scenario())


def test_mcp_action_tools_have_mcp_annotations():
    async def scenario():
        raw_tools = await actions_server.list_tools()
        tools_by_name = {t.name: t for t in raw_tools}
        # Destructive tools must carry destructive_hint / destructiveHint
        for name in ("crm_pause_principal", "crm_revoke_grant", "crm_undo_pause"):
            t = tools_by_name[name]
            ann = getattr(t, "annotations", None)
            assert ann is not None, f"{name} missing annotations"
            assert getattr(ann, "destructive_hint", False) is True or (isinstance(ann, dict) and ann.get("destructiveHint") is True)

        # Read tools must carry read_only_hint / readOnlyHint
        for name in ("crm_read", "crm_verify"):
            t = tools_by_name[name]
            ann = getattr(t, "annotations", None)
            assert ann is not None, f"{name} missing annotations"
            assert getattr(ann, "read_only_hint", False) is True or (isinstance(ann, dict) and ann.get("readOnlyHint") is True)
    asyncio.run(scenario())


def test_real_crm_operations_through_mcp_playbooks(tmp_path):
    from helio_crm.api import create_app as crm_app
    from test_graph import serve
    from test_playbooks_crm import make_tokens, EMPLOYEES

    tokens = make_tokens()
    app = crm_app(tmp_path / "crm.sqlite3", tokens, lambda: EMPLOYEES)
    with serve(app) as url:
        with patch.dict("os.environ", {
            "CRM_URL": url,
            "CRM_ACTION_TOKEN": tokens["actions"],
            "CRM_OBSERVER_TOKEN": tokens["observer"],
        }):
            pb = create_mcp_playbooks(actions_server)

            # 1. Read
            before = asyncio.run(pb["crm.pause_principal"].read("agent:sales-research"))
            assert before["principal_status"] == "active"
            assert isinstance(before["access_version"], int)

            # 2. Verify before mutation: expecting disabled is False
            assert asyncio.run(pb["crm.pause_principal"].verify("agent:sales-research", expected_state="disabled")) is False

            # 3. Run pause
            res = asyncio.run(pb["crm.pause_principal"].run("agent:sales-research", before_state=before))
            assert res["status"] == "executed"
            assert res["after"]["principal_status"] == "disabled"

            # 4. Verify after pause: expecting disabled is True, active is False
            assert asyncio.run(pb["crm.pause_principal"].verify("agent:sales-research", expected_state="disabled")) is True
            assert asyncio.run(pb["crm.pause_principal"].verify("agent:sales-research", expected_state="active")) is False

            # 5. Stale version failure
            with pytest.raises(PlaybookError, match="stale|changed"):
                asyncio.run(pb["crm.pause_principal"].run("agent:sales-research", force_version=before["access_version"]))

            # 6. Undo
            undo_res = asyncio.run(pb["crm.pause_principal"].undo("agent:sales-research", before=before))
            assert undo_res["status"] == "undone"
            assert asyncio.run(pb["crm.pause_principal"].verify("agent:sales-research", expected_state="active")) is True

            # 7. Revoke grant
            grant_id = "grant-sales-agent"
            grant_before = asyncio.run(pb["crm.revoke_grant"].read(grant_id))
            assert grant_before["status"] == "active"
            revoke_res = asyncio.run(pb["crm.revoke_grant"].run(grant_id, before_state=grant_before))
            assert revoke_res["status"] == "executed"
            assert asyncio.run(pb["crm.revoke_grant"].verify(grant_id, expected_state="revoked")) is True


