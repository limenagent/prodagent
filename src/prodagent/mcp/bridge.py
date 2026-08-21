from __future__ import annotations

import asyncio
import logging
import re
from typing import TYPE_CHECKING, Any

from prodagent.core.error_reason import ErrorReason
from prodagent.core.types import SideEffectLevel, ToolError, ToolMeta
from prodagent.mcp.transports import RPCError
from prodagent.tooling.base import FunctionTool

if TYPE_CHECKING:
    from prodagent.mcp.client import MCPClient, MCPToolInfo

logger = logging.getLogger(__name__)

_VALID_NAME = re.compile(r"[^A-Za-z0-9_-]")

# JSON-RPC error codes (black-box MCP error translation).
_RED_CODES = frozenset({-32601, -32602})


def _translate_mcp_error(exc: Exception, *, tool_name: str) -> ToolError:
    message = str(exc)
    if isinstance(exc, RPCError):
        if exc.code in _RED_CODES:
            return ToolError.from_reason(
                ErrorReason.FORMAT_ERROR,
                code="mcp_method_error",
                message=f"{tool_name}: {message}",
                hint="MCP rejected the call (method/params) — do not retry as-is.",
            )
        return ToolError.from_reason(
            ErrorReason.SERVER_ERROR,
            code="mcp_server_error",
            message=f"{tool_name}: {message}",
            hint="MCP server error — retry with backoff.",
        )
    if isinstance(exc, EOFError):
        return ToolError.from_reason(
            ErrorReason.CONNECTION,
            code="mcp_transport_closed",
            message=f"{tool_name}: {message}",
            hint="MCP transport closed — the server process exited or the stream was cut; retry may reconnect.",
        )
    return ToolError.from_reason(
        ErrorReason.CONNECTION,
        code="mcp_transport_error",
        message=f"{tool_name}: {message}",
        hint="MCP transport error — retry with backoff.",
    )


def qualified_name(server: str, tool: str) -> str:
    """Build ``mcp__<server>__<tool>``, sanitising chars outside [A-Za-z0-9_-]."""
    s = _VALID_NAME.sub("_", server)
    t = _VALID_NAME.sub("_", tool)
    return f"mcp__{s}__{t}"


async def adapt_mcp_tools(
    client: MCPClient,
    *,
    side_effect_level: SideEffectLevel = SideEffectLevel.MEDIUM,
    readonly_patterns: list[str] | None = None,
    max_concurrency: int = 8,
) -> list[FunctionTool]:
    timeout_ms = client.config.timeout_ms
    patterns = readonly_patterns or []
    tools = await client.list_tools()
    result: list[FunctionTool] = []
    for info in tools:
        is_ro = any(p in info.name.lower() for p in patterns)
        level = SideEffectLevel.LOW if is_ro else side_effect_level
        result.append(
            _make_tool(
                info,
                client,
                level=level,
                is_readonly=is_ro,
                max_concurrency=max_concurrency,
                timeout_ms=timeout_ms,
            )
        )
        logger.debug(
            "MCP tool bridged: %s (readonly=%s, level=%s)",
            qualified_name(info.server_name, info.name),
            is_ro,
            level.value,
        )
    logger.info(
        "MCP bridge: adapted %d tools from server %r",
        len(result),
        client.name,
    )
    return result


def _make_tool(
    info: MCPToolInfo,
    client: MCPClient,
    *,
    level: SideEffectLevel,
    is_readonly: bool,
    max_concurrency: int,
    timeout_ms: int,
) -> FunctionTool:
    qname = qualified_name(info.server_name, info.name)
    semaphore = asyncio.Semaphore(max_concurrency)
    meta = ToolMeta(
        name=qname,
        is_readonly=is_readonly,
        side_effect_level=level,
        domain=f"mcp:{info.server_name}",
        timeout_seconds=timeout_ms / 1_000,
    )
    schema = info.to_anthropic_schema()
    schema["name"] = qname

    async def _call(**kwargs: Any) -> Any:
        async with semaphore:
            try:
                result = await client.call_tool(info.name, kwargs)
                logger.debug("MCP tool %r returned: %s", qname, str(result)[:200])
                return result
            except Exception as exc:
                logger.error("MCP tool %r failed: %s", qname, exc)
                return _translate_mcp_error(exc, tool_name=qname).as_dict()

    return FunctionTool(name=qname, fn=_call, meta=meta, schema=schema)
