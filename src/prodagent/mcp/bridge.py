"""MCP layer 4 — the bridge: an anti-corruption layer, the real protagonist.

A remote MCP server is a black box with its own names, its own error codes,
and no idea what a ToolMeta is. Four translations happen here so that once
across the boundary, a remote tool is indistinguishable from a local one:
names get a ``mcp__<server>__<tool>`` prefix (no collisions, provenance
visible); risk is classified from the protocol's own annotations with a
conservative default (unknown ⇒ MEDIUM write); each tool carries its own
concurrency semaphore (a remote's slowness is throttled per tool, not
flooding the dispatcher); errors map from JSON-RPC codes onto the
framework's reasons so retry policy still means something. First-class
citizenship is not a special lane — it is this translation plus the same
dispatcher pipeline every local tool already flows through."""

from __future__ import annotations

import asyncio
import logging
import re
from typing import TYPE_CHECKING, Any

from prodagent.base.errors import ErrorReason
from prodagent.kernel.types import SideEffectLevel, ToolError, ToolMeta
from prodagent.mcp.transports import RPCError
from prodagent.tooling.base import FunctionTool

if TYPE_CHECKING:
    from prodagent.mcp.client import MCPClient, MCPToolInfo

logger = logging.getLogger(__name__)

_VALID_NAME = re.compile(r"[^A-Za-z0-9_-]")

# JSON-RPC error codes (black-box MCP error translation).
_RED_CODES = frozenset({-32601, -32602})


def _translate_mcp_error(exc: Exception, *, tool_name: str) -> ToolError:
    # Remote servers are black boxes with no shared error taxonomy; mapping
    # JSON-RPC codes onto the framework's reasons is what keeps retry policy
    # meaningful across the boundary.
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
    """Discover a server's tools and bridge each into a FunctionTool — the
    one function that turns "an MCP server" into "tools the dispatcher
    already knows how to schedule, gate, and break on"."""
    timeout_ms = client.config.timeout_ms
    patterns = readonly_patterns or []
    tools = await client.list_tools()
    result: list[FunctionTool] = []
    for info in tools:
        is_ro, level = _classify_risk(info, patterns=patterns, default_level=side_effect_level)
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


def _classify_risk(
    info: MCPToolInfo,
    *,
    patterns: list[str],
    default_level: SideEffectLevel,
) -> tuple[bool, SideEffectLevel]:
    """Prefer the MCP protocol's own annotations; fall back to substring matching
    only when the server didn't provide a hint."""
    if info.read_only_hint is True:
        return True, SideEffectLevel.LOW  # server's own annotation: parallel-safe
    if info.destructive_hint is True:
        return False, SideEffectLevel.HIGH  # server flagged destructive: approval territory
    if any(p in info.name.lower() for p in patterns):
        return True, SideEffectLevel.LOW  # name-based pattern match (caller's hint list)
    # No signal at all: conservative default — treated as a write (serial,
    # no approval unless the default level says otherwise).
    return False, default_level


def _make_tool(
    info: MCPToolInfo,
    client: MCPClient,
    *,
    level: SideEffectLevel,
    is_readonly: bool,
    max_concurrency: int,
    timeout_ms: int,
) -> FunctionTool:
    """Build one bridged tool: qualified name, translated meta (risk from
    the server's own annotations), server's schema under our name, and a
    call wrapper that never lets a remote exception escape — failures
    return as translated ``ToolError`` dicts the model can read."""
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
