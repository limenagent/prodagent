"""mcp —— 把 MCP Server 的工具在边界拉平，内部只留一条主路径（第 15 课）。

Agent 不该关心一个工具是本地 Python 函数，还是来自某个 MCP Server。这里做的
事情只有一件：把 MCP 端列出的工具，逐个注册成 ToolRegistry 里的普通 ToolSpec，
它们背后的 func 统一改成“通过 MCP 客户端调用”。于是校验、审批、幂等、结果
归一全都复用同一条工具管线，MCP 只是工具的又一个来源。

- InProcessMCPServer：进程内的 MCP 形态，离线测试/演示用；
- StdioMCPClient：通过子进程 + JSON-RPC 连接真实 MCP Server（标准库实现）。
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from typing import Any, Callable

from src.runtime.tools import ToolRegistry, ToolSpec


@dataclass
class McpToolInfo:
    name: str
    description: str
    parameters: dict
    handler: Callable


class InProcessMCPServer:
    """一个最小的进程内 MCP Server：工具名 -> (说明, schema, 处理函数)。"""

    def __init__(self, server_name: str = "inproc"):
        self.server_name = server_name
        self._tools: dict[str, McpToolInfo] = {}

    def define(self, name: str, handler: Callable, *, description: str = "",
               parameters: dict | None = None) -> "InProcessMCPServer":
        self._tools[name] = McpToolInfo(name, description,
                                        parameters or {"type": "object", "properties": {}}, handler)
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
    """异步把 MCP 工具挂到注册表里，返回导入的工具名。"""
    names = []
    for info in await server.list_tools():
        full_name = f"{prefix}{info.name}" if prefix else info.name

        def make_caller(tool_name: str):
            async def caller(arguments: dict, ctx: Any = None):
                return await server.call_tool(tool_name, arguments)
            return caller

        registry.add(ToolSpec(name=full_name, description=info.description,
                              func=make_caller(info.name), parameters=info.parameters,
                              side_effect="read"))
        names.append(full_name)
    return names


class StdioMCPClient:
    """通过 stdio + JSON-RPC 连接真实 MCP Server 的极简客户端（标准库实现）。

    教学版只覆盖 initialize / tools/list / tools/call 三个方法，足以说明
    “协议适配在边界完成”。生产可替换为官方 mcp SDK，对上暴露的仍是这两个方法。
    """

    def __init__(self, command: list[str]):
        self.command = command
        self._proc: asyncio.subprocess.Process | None = None
        self._id = 0

    async def __aenter__(self):
        self._proc = await asyncio.create_subprocess_exec(
            *self.command, stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE)
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
            raise RuntimeError(f"MCP {method} 失败：{response['error']}")
        return response.get("result", {})

    async def list_tools(self) -> list[McpToolInfo]:
        result = await self._rpc("tools/list", {})
        out = []
        for t in result.get("tools", []):
            out.append(McpToolInfo(t["name"], t.get("description", ""),
                                   t.get("inputSchema", {"type": "object", "properties": {}}), None))
        return out

    async def call_tool(self, name: str, arguments: dict) -> Any:
        result = await self._rpc("tools/call", {"name": name, "arguments": arguments})
        for content in result.get("content", []):
            if content.get("type") == "text":
                return content["text"]
        return result
