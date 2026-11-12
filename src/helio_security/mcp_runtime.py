"""Managed stdio MCP sessions with per-server configuration and cleanup."""
from contextlib import AsyncExitStack, asynccontextmanager
import os
from pathlib import Path

from fastmcp import Client
from fastmcp.client.transports import PythonStdioTransport
from langchain.mcp import MCPAdapter


@asynccontextmanager
async def mcp_connections():
    root = Path(__file__).resolve().parent
    async with AsyncExitStack() as stack:
        # Server diagnostics can contain source payloads; the worker reports
        # sanitized failures instead of forwarding subprocess stderr.
        diagnostics = stack.enter_context(open(os.devnull, "w"))
        adapters = []
        for filename, names in (
            ("mcp_read.py", ("SECURITY_API_URL", "SECURITY_VIEWER_TOKEN", "HELIO_COMPANY_ID", "WORKBENCH_URL", "WORKBENCH_OBSERVER_TOKEN")),
            ("mcp_actions.py", ("CRM_URL", "CRM_ACTION_TOKEN", "CRM_OBSERVER_TOKEN", "WORKBENCH_URL", "WORKBENCH_OBSERVER_TOKEN", "WORKBENCH_ACTION_TOKEN")),
        ):
            environment = {name: os.environ[name] for name in names if name in os.environ}
            environment["PYTHONPATH"] = str(root.parent)
            transport = PythonStdioTransport(root / filename, env=environment,
                                             keep_alive=False, log_file=diagnostics)
            adapter = MCPAdapter(Client(transport, timeout=15, init_timeout=15))
            adapters.append(await stack.enter_async_context(adapter))
        yield tuple(adapters)
