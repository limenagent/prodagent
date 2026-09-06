"""mcp — flatten MCP-server tools at the boundary; keep one main path inside.

An agent shouldn't care whether a tool is a local Python function or comes from
an MCP server. This module does one thing: register each tool listed by the MCP
side as an ordinary ToolSpec in the ToolRegistry, with its underlying func
uniformly changed to "call through the MCP client". Validation, approval,
idempotency, and result normalization then all reuse the same tool pipeline; MCP
is just one more source of tools.

- InProcessMCPServer: an in-process MCP form for offline tests/demos;
- StdioMCPClient: connects to a real MCP server over a subprocess + JSON-RPC
  (standard-library implementation).
"""

from __future__ import annotations

import asyncio
import json
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


class StdioMCPClient:
    """A minimal client to a real MCP server over stdio + JSON-RPC (stdlib only).

    The teaching build covers only initialize / tools/list / tools/call, enough
    to show "protocol adaptation happens at the boundary". In production swap in
    the official MCP SDK; the two methods exposed upward stay the same.
    """

    def __init__(self, command: list[str]):
        self.command = command
        self._proc: asyncio.subprocess.Process | None = None
        self._id = 0

    async def __aenter__(self):
        self._proc = await asyncio.create_subprocess_exec(
            *self.command, stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE
        )
        await self._rpc("initialize", {"protocolVersion": "2024-11-05"})
        return self

    async def __aexit__(self, *exc):
        if self._proc:
            self._proc.terminate()
            await self._proc.wait()

    async def _rpc(self, method: str, params: dict) -> dict:
        assert self._proc and self._proc.stdin and self._proc.stdout
        self._id += 1
        msg = {"jsonrpc": "2.0", "id": self._id, "method": method, "params": params}
        self._proc.stdin.write((json.dumps(msg) + "\n").encode())
        await self._proc.stdin.drain()
        line = await self._proc.stdout.readline()
        response = json.loads(line)
        if "error" in response:
            raise RuntimeError(f"MCP {method} failed: {response['error']}")
        return response.get("result", {})

    async def list_tools(self) -> list[McpToolInfo]:
        result = await self._rpc("tools/list", {})
        out = []
        for t in result.get("tools", []):
            out.append(
                McpToolInfo(
                    t["name"],
                    t.get("description", ""),
                    t.get("inputSchema", {"type": "object", "properties": {}}),
                    None,
                )
            )
        return out

    async def call_tool(self, name: str, arguments: dict) -> Any:
        result = await self._rpc("tools/call", {"name": name, "arguments": arguments})
        for content in result.get("content", []):
            if content.get("type") == "text":
                return content["text"]
        return result
