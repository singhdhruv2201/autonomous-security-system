"""Tests for read-only MCP tools and evidence sanitization."""
import pytest
from unittest.mock import patch, AsyncMock
import httpx

from helio_security.mcp_read import (
    read_server,
    get_finding,
    get_entity,
    get_neighbors,
    get_crm_activity,
    validate_id,
    validate_limit,
    redact_evidence,
)


from contextlib import contextmanager
from unittest.mock import MagicMock


@contextmanager
def mock_http(response=None, handler=None, error=None):
    """Exercise HTTPX streaming through its transport, not a mocked get method."""
    original = httpx.AsyncClient
    calls = MagicMock()
    async def handle(request):
        calls(request)
        if error:
            raise error
        if handler:
            return await handler(str(request.url))
        content = response.content
        if isinstance(content, bytes):
            return httpx.Response(response.status_code, content=content)
        return httpx.Response(response.status_code, json=response.json())
    with patch("httpx.AsyncClient", side_effect=lambda **kwargs: original(
            transport=httpx.MockTransport(handle), **kwargs)):
        yield calls


def test_validate_id_and_limit():
    assert validate_id("finding-123") == "finding-123"
    assert validate_id("agent:sales-research") == "agent:sales-research"
    assert validate_id("emp-maya-chen") == "emp-maya-chen"
    assert validate_id("grant.123") == "grant.123"

    for invalid in ("", "   ", "../secret", "http://evil.com", "id with spaces", "a" * 2500, "x;DROP TABLE"):
        with pytest.raises(ValueError, match="Invalid identifier"):
            validate_id(invalid)

    assert validate_limit(5) == 5
    assert validate_limit(20) == 20
    assert validate_limit(1) == 1
    with pytest.raises(ValueError, match="Invalid limit"):
        validate_limit(0)
    with pytest.raises(ValueError, match="Invalid limit"):
        validate_limit(21)


def test_redact_evidence_scrubs_secrets_and_pii():
    data = {
        "id": "agent:sales-research",
        "token": "xoxb-secret-token",
        "authorization": "Bearer secret",
        "customer_email": "alice@example.com",
        "customer_ssn": "000-00-0000",
        "record_count": 6,
        "nested": {
            "api_key": "secret_key",
            "safe_field": "public_value",
        },
    }
    redacted = redact_evidence(data)
    assert "token" not in redacted
    assert "authorization" not in redacted
    assert "customer_ssn" not in redacted
    assert redacted.get("record_count") == 6
    assert "api_key" not in redacted["nested"]
    assert redacted["nested"]["safe_field"] == "public_value"


import asyncio

from unittest.mock import patch, MagicMock

def test_get_finding_returns_compact_evidence():
    async def scenario():
        fake_finding = {
            "id": "finding-orphaned-agent",
            "company_id": "helio",
            "rule": "inactive_owner_agent_access",
            "severity": "high",
            "status": "open",
            "evidence": {
                "agent_id": "agent:sales-research",
                "employee_id": "emp-maya-chen",
                "employee_status": "inactive",
                "record_count": 6,
                "secret_token": "do-not-leak",
            },
            "coverage_complete": True,
        }

        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = fake_finding

        with mock_http(mock_resp):
            res = await get_finding("finding-orphaned-agent")
            assert res["id"] == "finding-orphaned-agent"
            assert res["rule"] == "inactive_owner_agent_access"
            assert "evidence" in res
            assert "secret_token" not in res["evidence"]
            assert res["evidence"]["record_count"] == 6
    asyncio.run(scenario())


def test_get_entity_and_get_neighbors():
    async def scenario():
        fake_entity = {"id": "agent:sales-research", "kind": "Agent", "properties": {"principal_status": "disabled"}}
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = fake_entity

        with mock_http(mock_resp):
            res = await get_entity("agent:sales-research")
            assert res["id"] == "agent:sales-research"
            assert res["kind"] == "Agent"

        fake_rel = {
            "nodes": [fake_entity, {"id": "grant:1", "kind": "AccessGrant"}],
            "edges": [{"kind": "HAS_GRANT", "start": "agent:sales-research", "end": "grant:1"}],
            "truncated": False,
            "coverage_complete": True,
        }
        mock_resp_rel = MagicMock()
        mock_resp_rel.status_code = 200
        mock_resp_rel.json.return_value = fake_rel

        with mock_http(mock_resp_rel):
            res_rel = await get_neighbors("agent:sales-research")
            assert len(res_rel["nodes"]) == 2
            assert len(res_rel["edges"]) == 1
            assert res_rel["truncated"] is False
    asyncio.run(scenario())


def test_get_crm_activity_bounds_limit():
    async def scenario():
        fake_activity = {
            "company_id": "helio",
            "items": [
                {"id": "ev-1", "actor_id": "agent:sales-research", "action": "customer.read", "record_count": 6}
            ],
            "total": 1,
        }
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = fake_activity

        with mock_http(mock_resp) as mock_get:
            res = await get_crm_activity("agent:sales-research", limit=10)
            assert len(res["items"]) == 1
            assert res["items"][0]["actor_id"] == "agent:sales-research"
            mock_get.assert_called_once()
    asyncio.run(scenario())


def test_api_unavailable_returns_explicit_unavailable_flag():
    async def scenario():
        with mock_http(error=httpx.ConnectError("Connection refused")):
            res = await get_finding("finding-123")
            assert res["unavailable"] is True
            assert "error" in res
    asyncio.run(scenario())


def test_read_server_tools_are_read_only_and_exclude_actions():
    async def scenario():
        raw_tools = await read_server.list_tools()
        tools = [t.name for t in raw_tools]
        assert set(tools) == {"get_finding", "get_entity", "get_neighbors", "get_crm_activity", "get_workbench_events", "probe_public_app"}
        # Check that no destructive/mutation action tools exist on read_server
        assert not any("pause" in name or "revoke" in name or "undo" in name for name in tools)
    asyncio.run(scenario())


def test_mcp_adapter_with_read_server():
    async def scenario():
        from langchain.mcp import MCPAdapter
        adapter = MCPAdapter(read_server)
        tools = await adapter.list_tools()
        names = {t.name for t in tools}
        assert names == {"get_finding", "get_entity", "get_neighbors", "get_crm_activity", "get_workbench_events", "probe_public_app"}
    asyncio.run(scenario())


def test_read_evidence_excludes_customer_values_in_free_text():
    import json
    raw = {
        "id": "activity-1",
        "notes": "Customer email REVIEW_SENTINEL@example.test, secret sk-review-SENTINEL",
        "email": "REVIEW_SENTINEL@example.test",
    }
    redacted = redact_evidence(raw)
    assert "REVIEW_SENTINEL" not in json.dumps(redacted)


def test_oversized_response_rejected_by_ceiling():
    async def scenario():
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.content = b"x" * 70000  # > 64 KB ceiling

        with mock_http(mock_resp):
            res = await get_finding("finding-oversized")
            assert res.get("unavailable") is True
            assert res.get("error") == "response_too_large"
    asyncio.run(scenario())


def test_graph_traversal_finding_to_entity_to_relationships():
    async def scenario():
        finding_data = {
            "id": "finding-orphaned-agent",
            "rule": "inactive_owner_agent_access",
            "evidence": {"agent_id": "agent:sales-research", "employee_id": "emp-maya-chen"},
        }
        entity_data = {
            "id": "WyJoZWxpbyIsIkFnZW50IiwiYWdlbnQ6c2FsZXMtcmVzZWFyY2giXQ",
            "kind": "Agent",
            "properties": {"name": "Sales Research Agent", "status": "active"},
        }
        neighbors_data = {
            "nodes": [
                entity_data,
                {"id": "WyJoZWxpbyIsIkVtcGxveWVlIiwiZW1wLW1heWEtY2hlbiJd", "kind": "Employee"},
                {"id": "WyJoZWxpbyIsIkdyYW50IiwiZ3JhbnQtc2FsZXMiXQ", "kind": "AccessGrant"},
            ],
            "edges": [
                {"kind": "OWNED_BY", "start": entity_data["id"], "end": "WyJoZWxpbyIsIkVtcGxveWVlIiwiZW1wLW1heWEtY2hlbiJd"},
                {"kind": "HAS_GRANT", "start": entity_data["id"], "end": "WyJoZWxpbyIsIkdyYW50IiwiZ3JhbnQtc2FsZXMiXQ"},
            ],
            "truncated": False,
        }

        async def fake_get(url, *args, **kwargs):
            url_str = str(url)
            if "/findings/" in url_str:
                return httpx.Response(200, json=finding_data)
            elif "/relationships" in url_str:
                return httpx.Response(200, json=neighbors_data)
            elif "/entities/" in url_str:
                return httpx.Response(200, json=entity_data)
            return httpx.Response(404, json={"error": "not found"})

        with mock_http(handler=fake_get):
            # 1. Inspect finding
            finding = await get_finding("finding-orphaned-agent")
            agent_source_id = finding["evidence"]["agent_id"]
            assert agent_source_id == "agent:sales-research"

            # 2. Look up agent entity using source ID (resolves to canonical graph ID)
            agent_entity = await get_entity(agent_source_id)
            assert agent_entity["kind"] == "Agent"

            # 3. Look up neighbors of agent entity
            neighbors = await get_neighbors(agent_source_id)
            assert len(neighbors["nodes"]) == 3
            kinds = {n["kind"] for n in neighbors["nodes"]}
            assert "Employee" in kinds
            assert "AccessGrant" in kinds

    asyncio.run(scenario())



