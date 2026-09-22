"""mcp — flatten MCP-server tools at the boundary; keep one main path inside.

An agent shouldn't care whether a tool is a local Python function or comes from
an MCP server. This module does one thing: register each tool listed by the MCP
side as an ordinary ToolSpec in the ToolRegistry, with its underlying func
uniformly changed to "call through the MCP client". Validation, approval,
idempotency, and result normalization then all reuse the same tool pipeline; MCP
is just one more source of tools.

InProcessMCPServer is an in-process MCP form for offline tests/demos; a real
client (stdio/HTTP) only has to offer the same list_tools / call_tool pair.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from src.runtime.tools import ToolRegistry, ToolSpec


@dataclass
class McpToolInfo:
    name: str
    description: str
    parameters: dict
    handler: Callable


class InProcessMCPServer:
    """A minimal in-process MCP server: tool name -> (description, schema, handler)."""

    def __init__(self, server_name: str = "inproc"):
        self.server_name = server_name
        self._tools: dict[str, McpToolInfo] = {}

    def define(
        self, name: str, handler: Callable, *, description: str = "", parameters: dict | None = None
    ) -> InProcessMCPServer:
        self._tools[name] = McpToolInfo(
            name, description, parameters or {"type": "object", "properties": {}}, handler
        )
        return self

    async def list_tools(self) -> list[McpToolInfo]:
        return list(self._tools.values())

    async def call_tool(self, name: str, arguments: dict) -> Any:
        info = self._tools[name]
        result = info.handler(arguments)
        if asyncio.iscoroutine(result):
            result = await result
        return result


async def load_mcp_tools(registry: ToolRegistry, server: Any, *, prefix: str = "") -> list[str]:
    """Asynchronously attach MCP tools to the registry; return the imported tool names."""
    names = []
    for info in await server.list_tools():
        full_name = f"{prefix}{info.name}" if prefix else info.name

        def make_caller(tool_name: str):
            async def caller(arguments: dict, ctx: Any = None):
                return await server.call_tool(tool_name, arguments)

            return caller

        registry.add(
            ToolSpec(
                name=full_name,
                description=info.description,
                func=make_caller(info.name),
                parameters=info.parameters,
                side_effect="read",
            )
        )
        names.append(full_name)
    return names
