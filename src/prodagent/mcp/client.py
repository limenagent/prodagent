from __future__ import annotations

import contextlib
import logging
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from prodagent.mcp.transports import HttpTransport, StdioTransport

if TYPE_CHECKING:
    from prodagent.mcp.config import MCPServerConfig

logger = logging.getLogger(__name__)

_PROTOCOL_VERSION = "2025-06-18"
_CLIENT_NAME = "prodagent"
_CLIENT_VERSION = "1.0.0"


@dataclass
class MCPToolInfo:
    name: str
    description: str
    input_schema: dict[str, Any] = field(default_factory=dict)
    server_name: str = ""
    read_only_hint: bool | None = None
    destructive_hint: bool | None = None
    idempotent_hint: bool | None = None

    def to_anthropic_schema(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "input_schema": self.input_schema
            or {
                "type": "object",
                "properties": {},
            },
        }


class MCPClient:
    """Async client for a single MCP server."""

    def __init__(self, config: MCPServerConfig) -> None:
        self._config = config
        self._name = config.name
        self._transport: StdioTransport | HttpTransport = _build_transport(config)
        self._tools: list[MCPToolInfo] | None = None
        self._connected = False

    @property
    def name(self) -> str:
        return self._name

    @property
    def config(self) -> MCPServerConfig:
        return self._config

    @property
    def connected(self) -> bool:
        return self._connected

    async def connect(self) -> None:
        if self._connected:
            return
        await _connect_transport(self._transport, self._config)
        try:
            await self._handshake()
        except BaseException:
            with contextlib.suppress(Exception):
                await self._transport.close()
            self._transport = _build_transport(self._config)
            raise
        self._connected = True

    async def close(self) -> None:
        if self._connected:
            await self._transport.close()
            self._transport = _build_transport(self._config)
            self._tools = None
            self._connected = False

    async def _handshake(self) -> None:
        result = await self._transport.send(
            "initialize",
            {
                "protocolVersion": _PROTOCOL_VERSION,
                "capabilities": {"tools": {}},
                "clientInfo": {"name": _CLIENT_NAME, "version": _CLIENT_VERSION},
            },
        )
        server_version = result.get("protocolVersion", _PROTOCOL_VERSION)
        if server_version != _PROTOCOL_VERSION:
            logger.info(
                "MCP server %r negotiated protocol %s (client prefers %s) — continuing",
                self._name,
                server_version,
                _PROTOCOL_VERSION,
            )
        server_name = result.get("serverInfo", {}).get("name", "?")
        logger.info("MCP handshake complete: server=%s protocol=%s", server_name, server_version)
        await self._transport.notify("notifications/initialized")

    async def list_tools(self, *, refresh: bool = False) -> list[MCPToolInfo]:
        if self._tools is not None and not refresh:
            return self._tools

        result = await self._transport.send("tools/list", {})
        raw_tools = result.get("tools", [])
        self._tools = [
            MCPToolInfo(
                name=t["name"],
                description=t.get("description", ""),
                input_schema=t.get("inputSchema", {}),
                server_name=self._name,
                read_only_hint=t.get("annotations", {}).get("readOnlyHint"),
                destructive_hint=t.get("annotations", {}).get("destructiveHint"),
                idempotent_hint=t.get("annotations", {}).get("idempotentHint"),
            )
            for t in raw_tools
        ]
        logger.info("MCP server %r exposes %d tools", self._name, len(self._tools))
        return self._tools

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> Any:
        result = await self._transport.send("tools/call", {"name": name, "arguments": arguments})
        content = result.get("content", [])
        parts: list[str] = []
        for block in content:
            if block.get("type") == "text":
                parts.append(block.get("text", ""))
            elif block.get("type") == "resource":
                parts.append(str(block.get("resource", "")))
        text = "\n".join(parts) if parts else ""

        if result.get("isError"):
            from prodagent.core.error_reason import ErrorReason
            from prodagent.kernel.types import ToolError

            logger.warning("MCP tool %r returned isError=true: %s", name, text[:200])
            return ToolError.from_reason(
                ErrorReason.UNKNOWN,
                code="mcp_tool_error",
                message=text or f"MCP tool {name!r} returned an error",
                hint="The MCP server signalled failure; do not retry without changing arguments.",
            ).as_dict()

        if not parts:
            logger.warning("MCP tool %r returned no text/resource blocks: %s", name, result)
            return ""
        return text

    async def __aenter__(self) -> MCPClient:
        await self.connect()
        return self

    async def __aexit__(self, *_: Any) -> None:
        await self.close()


def _build_transport(config: MCPServerConfig) -> StdioTransport | HttpTransport:
    if config.transport == "stdio":
        return StdioTransport(read_timeout=config.call_timeout)
    if config.transport == "http":
        return HttpTransport(timeout=config.call_timeout)
    raise ValueError(f"Unknown MCP transport: {config.transport!r}")


async def _connect_transport(
    transport: StdioTransport | HttpTransport, config: MCPServerConfig
) -> None:
    if isinstance(transport, StdioTransport):
        await transport.connect([config.command, *config.args], env=config.env or None)
    else:
        await transport.connect(config.url, headers=config.headers or None)
