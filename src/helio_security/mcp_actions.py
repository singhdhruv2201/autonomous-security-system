"""Action MCP server exposing CRM remediation and verification tools to the deterministic workflow."""
import asyncio
import json
import os
from typing import Any, Dict, Optional

from fastmcp import FastMCP
from mcp.types import ToolAnnotations

try:
    from .playbooks import REGISTRY, get_playbook, PlaybookError, UnknownOutcome
except ImportError:
    import sys
    from pathlib import Path
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from helio_security.playbooks import REGISTRY, get_playbook, PlaybookError, UnknownOutcome

actions_server = FastMCP("awasi-actions")


def _get_crm_config():
    crm_url = os.getenv("CRM_URL", "http://127.0.0.1:8001").rstrip("/")
    action_token = os.getenv("CRM_ACTION_TOKEN", "")
    observer_token = os.getenv("CRM_OBSERVER_TOKEN", "")
    return crm_url, action_token, observer_token


async def _mutation(method, *args, **kwargs):
    try:
        return await method(*args, **kwargs)
    except UnknownOutcome:
        return {"status": "unknown"}
    except PlaybookError:
        return {"status": "rejected"}


@actions_server.tool(
    tags=["destructive"],
    annotations=ToolAnnotations(destructive_hint=True),
)
async def crm_pause_principal(
    target: str,
    force_version: Optional[int] = None,
    before_state: Optional[dict] = None,
) -> Dict[str, Any]:
    """Destructive mutation: pause CRM access for an agent principal."""
    crm_url, action_token, observer_token = _get_crm_config()
    pb = REGISTRY["crm.pause_principal"]
    return await _mutation(pb.run,
        target,
        crm_url=crm_url,
        action_token=action_token,
        observer_token=observer_token,
        force_version=force_version,
        before_state=before_state,
    )


@actions_server.tool(
    tags=["destructive"],
    annotations=ToolAnnotations(destructive_hint=True),
)
async def crm_undo_pause(
    target: str,
    before: dict,
    before_state: Optional[dict] = None,
) -> Dict[str, Any]:
    """Destructive mutation: restore CRM principal access using saved original state."""
    crm_url, action_token, observer_token = _get_crm_config()
    pb = REGISTRY["crm.pause_principal"]
    return await _mutation(pb.undo,
        target,
        before=before,
        crm_url=crm_url,
        action_token=action_token,
        observer_token=observer_token,
        before_state=before_state,
    )


@actions_server.tool(
    tags=["destructive"],
    annotations=ToolAnnotations(destructive_hint=True),
)
async def crm_revoke_grant(
    target: str,
    force_version: Optional[int] = None,
    before_state: Optional[dict] = None,
) -> Dict[str, Any]:
    """Destructive mutation: permanently revoke an agent's CRM access grant."""
    crm_url, action_token, observer_token = _get_crm_config()
    pb = REGISTRY["crm.revoke_grant"]
    return await _mutation(pb.run,
        target,
        crm_url=crm_url,
        action_token=action_token,
        observer_token=observer_token,
        force_version=force_version,
        before_state=before_state,
    )


@actions_server.tool(
    tags=["read_only"],
    annotations=ToolAnnotations(read_only_hint=True),
)
async def crm_read(
    playbook_id: str,
    target: str,
) -> Dict[str, Any]:
    """Read observable pre-state of a target principal or grant."""
    crm_url, _, observer_token = _get_crm_config()
    pb = get_playbook(playbook_id)
    return await pb.read(target, crm_url=crm_url, observer_token=observer_token)


@actions_server.tool(
    tags=["read_only"],
    annotations=ToolAnnotations(read_only_hint=True),
)
async def crm_verify(
    playbook_id: str,
    target: str,
    expected_state: Optional[str] = None,
) -> bool:
    """Verify observable post-state of a target principal or grant."""
    crm_url, _, observer_token = _get_crm_config()
    pb = get_playbook(playbook_id)
    kwargs = {"expected_state": expected_state} if expected_state is not None else {}
    return await pb.verify(target, crm_url=crm_url, observer_token=observer_token, **kwargs)


def _workbench_config():
    return {"workbench_url":os.getenv("WORKBENCH_URL", "http://127.0.0.1:8004"),
            "workbench_observer_token":os.getenv("WORKBENCH_OBSERVER_TOKEN", ""),
            "workbench_action_token":os.getenv("WORKBENCH_ACTION_TOKEN", "")}


def _workbench_book(playbook_id):
    if not playbook_id.startswith("workbench."):
        raise PlaybookError("Not a workbench playbook")
    return get_playbook(playbook_id)


@actions_server.tool(tags=["destructive"], annotations=ToolAnnotations(destructive_hint=True))
async def workbench_run(playbook_id: str, target: str, before_state: Optional[dict] = None) -> dict:
    """Apply an allowlisted versioned workbench fix; deterministic workflow only."""
    return await _mutation(_workbench_book(playbook_id).run, target, before_state=before_state, **_workbench_config())


@actions_server.tool(tags=["destructive"], annotations=ToolAnnotations(destructive_hint=True))
async def workbench_undo(playbook_id: str, target: str, before: dict, before_state: Optional[dict] = None) -> dict:
    """Restore the saved state of a versioned workbench target."""
    return await _mutation(_workbench_book(playbook_id).undo, target, before=before, before_state=before_state, **_workbench_config())


@actions_server.tool(tags=["read_only"], annotations=ToolAnnotations(read_only_hint=True))
async def workbench_read(playbook_id: str, target: str) -> dict:
    """Read the pre-state needed for a durable action."""
    return await _workbench_book(playbook_id).read(target, **_workbench_config())


@actions_server.tool(tags=["read_only"], annotations=ToolAnnotations(read_only_hint=True))
async def workbench_verify(playbook_id: str, target: str, expected_state: Optional[str | bool] = None) -> bool:
    """Verify a workbench post-state, including anonymous denial for restricted apps."""
    return await _workbench_book(playbook_id).verify(target, expected_state=expected_state, **_workbench_config())


def _decode_mcp_result(res: Any) -> Any:
    """Normalize supported FastMCP / MCP result formats into application values."""
    if res is None:
        return None

    # Check for protocol or tool errors
    if getattr(res, "is_error", False) or getattr(res, "isError", False):
        raise UnknownOutcome("MCP tool outcome unavailable")

    # Check for FastMCP structured_content
    struct = getattr(res, "structured_content", None)
    if struct is not None:
        if isinstance(struct, dict) and list(struct.keys()) == ["result"]:
            return struct["result"]
        return struct

    # Check for data attribute
    data = getattr(res, "data", None)
    if data is not None:
        return data

    # Check content blocks (mcp CallToolResult or TextContent)
    content = getattr(res, "content", None)
    if isinstance(content, list):
        for block in content:
            text = getattr(block, "text", None)
            if isinstance(text, str):
                text_clean = text.strip()
                if text_clean.lower() == "false":
                    return False
                if text_clean.lower() == "true":
                    return True
                try:
                    parsed = json.loads(text)
                    if isinstance(parsed, dict) and list(parsed.keys()) == ["result"]:
                        return parsed["result"]
                    return parsed
                except (json.JSONDecodeError, TypeError):
                    return text_clean

    return res


class MCPPlaybook:
    """Wraps an MCP adapter so the workflow dispatch boundary invokes action tools over MCP."""

    def __init__(self, playbook_id: str, reversible: bool, adapter_or_server: Any):
        self.id = playbook_id
        self.reversible = reversible
        self._target = adapter_or_server
        self.state_field = getattr(REGISTRY[playbook_id], "state_field", "principal_status")
        self.workbench = playbook_id.startswith("workbench.")

    async def _call_tool(self, name: str, **kwargs) -> Any:
        filtered = {k: v for k, v in kwargs.items() if v is not None}
        try:
            if hasattr(self._target, "client") and hasattr(self._target.client, "call_tool"):
                res = await self._target.client.call_tool(name, filtered)
                return _decode_mcp_result(res)
            if hasattr(self._target, "call_tool"):
                res = await self._target.call_tool(name, filtered)
                return _decode_mcp_result(res)
            raise PlaybookError(f"Target adapter {type(self._target).__name__} does not support call_tool")
        except PlaybookError:
            raise
        except Exception:
            if name in {"crm_pause_principal", "crm_revoke_grant", "crm_undo_pause", "workbench_run", "workbench_undo"}:
                raise UnknownOutcome("MCP mutation response unavailable") from None
            raise PlaybookError("MCP read unavailable") from None

    async def read(
        self,
        target: str,
        **kwargs,
    ) -> dict:
        decoded = await self._call_tool(
            "workbench_read" if self.workbench else "crm_read",
            playbook_id=self.id,
            target=target,
        )
        if not isinstance(decoded, dict):
            raise PlaybookError(f"crm_read returned invalid result type: {type(decoded).__name__}")
        return decoded

    async def run(
        self,
        target: str,
        force_version: Optional[int] = None,
        before_state: Optional[dict] = None,
        **kwargs,
    ) -> dict:
        tool_name = "crm_pause_principal" if self.id == "crm.pause_principal" else "crm_revoke_grant"
        extra = {"playbook_id":self.id} if self.workbench else {"force_version":force_version}
        decoded = await self._call_tool(
            "workbench_run" if self.workbench else tool_name,
            target=target,
            **extra,
            before_state=before_state,
        )
        if not isinstance(decoded, dict) or "status" not in decoded:
            raise UnknownOutcome("MCP mutation returned invalid result")
        self._require_success(decoded)
        return decoded

    async def undo(
        self,
        target: str,
        before: dict,
        before_state: Optional[dict] = None,
        **kwargs,
    ) -> dict:
        if not self.reversible:
            raise PlaybookError(f"{self.id} is not reversible")
        decoded = await self._call_tool(
            "workbench_undo" if self.workbench else "crm_undo_pause",
            **({"playbook_id":self.id} if self.workbench else {}),
            target=target,
            before=before,
            before_state=before_state,
        )
        if not isinstance(decoded, dict) or "status" not in decoded:
            raise UnknownOutcome("MCP undo returned invalid result")
        self._require_success(decoded)
        return decoded

    @staticmethod
    def _require_success(result):
        if result.get("status") == "rejected":
            raise PlaybookError("Action rejected; target may have changed")
        if result.get("status") not in {"executed", "undone"}:
            raise UnknownOutcome("MCP mutation outcome unknown")

    async def verify(
        self,
        target: str,
        expected_state: Optional[str] = None,
        **kwargs,
    ) -> bool:
        decoded = await self._call_tool(
            "workbench_verify" if self.workbench else "crm_verify",
            playbook_id=self.id,
            target=target,
            expected_state=expected_state,
        )
        if not isinstance(decoded, bool):
            raise PlaybookError(f"crm_verify returned non-boolean result: {decoded!r}")
        return decoded


def create_mcp_playbooks(adapter_or_server: Any) -> Dict[str, MCPPlaybook]:
    """Create playbook instances backed by the MCP action server."""
    return {identifier:MCPPlaybook(identifier, reversible=pb.reversible, adapter_or_server=adapter_or_server)
            for identifier,pb in REGISTRY.items()}



if __name__ == "__main__":
    asyncio.run(actions_server.run_stdio_async())
