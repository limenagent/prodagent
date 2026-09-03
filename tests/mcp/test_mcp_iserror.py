from __future__ import annotations

import json
from typing import Any

import httpx
import pytest

from prodagent.kernel.body import coerce_result
from prodagent.kernel.types import ErrorSeverity, ToolOutcome
from prodagent.mcp.client import MCPClient
from prodagent.mcp.config import MCPServerConfig


def _json_response(req_id: int, result: dict[str, Any]) -> httpx.Response:
    return httpx.Response(
        200,
        content=json.dumps({"jsonrpc": "2.0", "id": req_id, "result": result}).encode(),
        headers={"content-type": "application/json"},
    )


def _sse_response(req_id: int, result: dict[str, Any]) -> httpx.Response:
    payload = json.dumps({"jsonrpc": "2.0", "id": req_id, "result": result})
    return httpx.Response(
        200,
        content=f"event: message\ndata: {payload}\n\n".encode(),
        headers={"content-type": "text/event-stream"},
    )


def _error_server_handler() -> Any:

    async def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        req_id = body["id"]
        method = body["method"]

        if method == "initialize":
            return _json_response(
                req_id,
                {
                    "protocolVersion": "2025-06-18",
                    "serverInfo": {"name": "mock", "version": "1.0"},
                    "capabilities": {"tools": {}},
                },
            )
        if method == "tools/list":
            return _sse_response(
                req_id,
                {"tools": [{"name": "delete", "description": "d", "inputSchema": {}}]},
            )
        if method == "tools/call":
            return _sse_response(
                req_id,
                {
                    "isError": True,
                    "content": [
                        {"type": "text", "text": "permission denied: role 'guest' cannot delete"}
                    ],
                },
            )
        return _json_response(req_id, {})

    return handler


def _patch_client_session(client: MCPClient, handler: Any) -> None:
    transport = client._transport  # type: ignore[attr-defined]
    transport._session = httpx.AsyncClient(transport=httpx.MockTransport(handler))  # type: ignore[attr-defined]
    transport._url = client.config.url  # type: ignore[attr-defined]
    client._connected = True  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_mcp_iserror_returns_red_tool_error_dict() -> None:
    cfg = MCPServerConfig(name="mock", transport="http", url="http://x/mcp")
    client = MCPClient(cfg)
    _patch_client_session(client, _error_server_handler())

    result = await client.call_tool("delete", {"id": "r-42"})

    assert isinstance(result, dict)
    assert result.get("code") == "mcp_tool_error"
    assert result.get("error_severity") == ErrorSeverity.RED.value
    assert "permission denied" in result.get("message", "")
    await client.close()


@pytest.mark.asyncio
async def test_mcp_iserror_routes_to_abort_via_from_raw() -> None:
    cfg = MCPServerConfig(name="mock", transport="http", url="http://x/mcp")
    client = MCPClient(cfg)
    _patch_client_session(client, _error_server_handler())

    raw = await client.call_tool("delete", {"id": "r-42"})
    tr = coerce_result(raw, tool="delete")

    assert tr.outcome is ToolOutcome.ABORT
    assert tr.error is not None
    assert tr.error.error_severity is ErrorSeverity.RED
    await client.close()


@pytest.mark.asyncio
async def test_mcp_success_still_returns_plain_text() -> None:

    async def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        req_id = body["id"]
        method = body["method"]
        if method == "initialize":
            return _json_response(
                req_id,
                {
                    "protocolVersion": "2025-06-18",
                    "serverInfo": {"name": "mock", "version": "1.0"},
                    "capabilities": {"tools": {}},
                },
            )
        if method == "tools/list":
            return _sse_response(
                req_id,
                {"tools": [{"name": "search", "description": "d", "inputSchema": {}}]},
            )
        if method == "tools/call":
            return _sse_response(
                req_id,
                {"content": [{"type": "text", "text": "ok: 3 hits"}]},
            )
        return _json_response(req_id, {})

    cfg = MCPServerConfig(name="mock", transport="http", url="http://x/mcp")
    client = MCPClient(cfg)
    _patch_client_session(client, handler)

    result = await client.call_tool("search", {"q": "x"})
    assert result == "ok: 3 hits"
    await client.close()
