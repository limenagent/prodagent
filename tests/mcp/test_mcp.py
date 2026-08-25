from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

import httpx
import pytest

from prodagent.mcp import (
    MCPClient,
    MCPRegistry,
    MCPServerConfig,
    adapt_mcp_tools,
    expand_env,
    load_mcp_servers,
)
from prodagent.mcp.transports import RPCError
from prodagent.mcp.transports.http import HttpTransport

if TYPE_CHECKING:
    from prodagent.tooling.base import FunctionTool


class TestConfig:
    def test_stdio_from_dict(self) -> None:
        cfg = MCPServerConfig.from_dict("git", {"command": "uvx", "args": ["mcp-server-git"]})
        assert cfg.transport == "stdio"
        assert cfg.command == "uvx"
        assert cfg.args == ["mcp-server-git"]

    def test_http_from_dict(self) -> None:
        cfg = MCPServerConfig.from_dict("rca", {"type": "http", "url": "http://x/mcp"})
        assert cfg.transport == "http"
        assert cfg.url == "http://x/mcp"

    def test_streamable_http_alias(self) -> None:
        cfg = MCPServerConfig.from_dict("s", {"type": "streamable-http", "url": "http://x"})
        assert cfg.transport == "http"

    def test_sse_alias(self) -> None:
        cfg = MCPServerConfig.from_dict("s", {"type": "sse", "url": "https://x/sse"})
        assert cfg.transport == "http"

    def test_url_without_type_rejected(self) -> None:
        with pytest.raises(ValueError, match="has 'url' but no 'type'"):
            MCPServerConfig.from_dict("bad", {"url": "http://x"})

    def test_unknown_transport_rejected(self) -> None:
        with pytest.raises(ValueError, match="unknown transport type"):
            MCPServerConfig.from_dict("bad", {"type": "ws", "url": "http://x"})

    def test_stdio_requires_command(self) -> None:
        with pytest.raises(ValueError, match="requires a 'command'"):
            MCPServerConfig(name="bad", transport="stdio")

    def test_timeout_floor(self) -> None:
        with pytest.raises(ValueError, match="timeout_ms must be >= 1000"):
            MCPServerConfig(name="bad", transport="http", url="http://x", timeout_ms=500)

    def test_expand_env_simple(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("FOO", "bar")
        assert expand_env("${FOO}") == "bar"

    def test_expand_env_in_headers(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("TOKEN", "secret")
        cfg = MCPServerConfig.from_dict(
            "s",
            {
                "type": "http",
                "url": "http://x",
                "headers": {"Authorization": "Bearer ${TOKEN}"},
            },
        )
        assert cfg.headers["Authorization"] == "Bearer secret"

    def test_load_mcp_servers_dict(self) -> None:
        configs = load_mcp_servers(
            {
                "mcpServers": {
                    "a": {"type": "http", "url": "http://a"},
                    "b": {"command": "uvx", "args": ["b"]},
                }
            }
        )
        assert len(configs) == 2
        assert {c.name for c in configs} == {"a", "b"}

    def test_load_filters_disabled(self) -> None:
        configs = load_mcp_servers(
            {
                "mcpServers": {
                    "a": {"type": "http", "url": "http://a", "enabled": False},
                    "b": {"type": "http", "url": "http://b"},
                }
            }
        )
        assert [c.name for c in configs] == ["b"]


def _sse_response(req_id: int | str, result: dict[str, Any]) -> httpx.Response:
    payload = {"jsonrpc": "2.0", "id": req_id, "result": result}
    body = f"data: {json.dumps(payload)}\n\n".encode()
    return httpx.Response(
        200,
        headers={"content-type": "text/event-stream"},
        content=body,
    )


def _json_response(req_id: int | str, result: dict[str, Any]) -> httpx.Response:
    return httpx.Response(
        200,
        headers={"content-type": "application/json"},
        content=json.dumps({"jsonrpc": "2.0", "id": req_id, "result": result}).encode(),
    )


def _make_http_client(handler: Any) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


class TestHttpTransport:
    async def test_sse_response_parsed(self) -> None:
        async def handler(request: httpx.Request) -> httpx.Response:
            body = json.loads(request.content)
            return _sse_response(body["id"], {"tools": [{"name": "ping"}]})

        t = HttpTransport()
        t._session = _make_http_client(handler)
        t._url = "http://x/mcp"
        result = await t.send("tools/list", {})
        assert result == {"tools": [{"name": "ping"}]}
        await t.close()

    async def test_json_response_parsed(self) -> None:
        async def handler(request: httpx.Request) -> httpx.Response:
            body = json.loads(request.content)
            return _json_response(body["id"], {"ok": True})

        t = HttpTransport()
        t._session = _make_http_client(handler)
        t._url = "http://x/mcp"
        result = await t.send("ping", {})
        assert result == {"ok": True}
        await t.close()

    async def test_rpc_error_raises(self) -> None:
        async def handler(request: httpx.Request) -> httpx.Response:
            body = json.loads(request.content)
            err = {"jsonrpc": "2.0", "id": body["id"], "error": {"code": -32600, "message": "bad"}}
            return httpx.Response(
                200,
                headers={"content-type": "application/json"},
                content=json.dumps(err).encode(),
            )

        t = HttpTransport()
        t._session = _make_http_client(handler)
        t._url = "http://x/mcp"
        with pytest.raises(RPCError, match="MCP RPC error -32600"):
            await t.send("boom", {})
        await t.close()

    async def test_sse_ignores_non_matching_lines(self) -> None:

        async def handler(request: httpx.Request) -> httpx.Response:
            body = json.loads(request.content)
            req_id = body["id"]
            noise1 = (
                f"data: {json.dumps({'jsonrpc': '2.0', 'method': 'notifications/progress'})}\n\n"
            )
            noise2 = (
                f"data: {json.dumps({'jsonrpc': '2.0', 'id': 999, 'result': {'other': True}})}\n\n"
            )
            ours = (
                f"data: {json.dumps({'jsonrpc': '2.0', 'id': req_id, 'result': {'won': True}})}\n\n"
            )
            return httpx.Response(
                200,
                headers={"content-type": "text/event-stream"},
                content=(noise1 + noise2 + ours).encode(),
            )

        t = HttpTransport()
        t._session = _make_http_client(handler)
        t._url = "http://x/mcp"
        result = await t.send("tools/call", {"name": "x"})
        assert result == {"won": True}
        await t.close()


def _mock_server_handler(
    tools: list[dict[str, Any]],
    call_results: dict[str, Any] | None = None,
) -> Any:
    call_results = call_results or {}

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
            return _sse_response(req_id, {"tools": tools})
        if method == "tools/call":
            tool_name = body["params"]["name"]
            args = body["params"].get("arguments", {})
            out = call_results.get(tool_name, {"echo": args})
            return _sse_response(
                req_id,
                {
                    "content": [{"type": "text", "text": json.dumps(out)}],
                },
            )
        return _json_response(req_id, {})

    return handler


def _patch_client_session(client: MCPClient, handler: Any) -> None:
    transport = client._transport  # type: ignore[attr-defined]
    transport._session = _make_http_client(handler)  # type: ignore[attr-defined]
    transport._url = client.config.url  # type: ignore[attr-defined]
    client._connected = True  # type: ignore[attr-defined]


class TestMCPClient:
    async def test_list_tools_and_call(self) -> None:
        cfg = MCPServerConfig(name="mock", transport="http", url="http://x/mcp")
        client = MCPClient(cfg)
        _patch_client_session(
            client,
            _mock_server_handler(
                tools=[{"name": "search", "description": "d", "inputSchema": {}}],
                call_results={"search": {"hits": 3}},
            ),
        )
        tools = await client.list_tools()
        assert len(tools) == 1
        assert tools[0].name == "search"
        assert tools[0].server_name == "mock"

        result = await client.call_tool("search", {"q": "x"})
        assert "hits" in result
        await client.close()

    async def test_list_tools_parses_annotations(self) -> None:
        cfg = MCPServerConfig(name="mock", transport="http", url="http://x/mcp")
        client = MCPClient(cfg)
        _patch_client_session(
            client,
            _mock_server_handler(
                tools=[
                    {
                        "name": "search",
                        "description": "d",
                        "inputSchema": {},
                        "annotations": {
                            "readOnlyHint": True,
                            "destructiveHint": False,
                            "idempotentHint": True,
                        },
                    },
                    {"name": "plain", "description": "d", "inputSchema": {}},
                ]
            ),
        )
        tools = await client.list_tools()
        by_name = {t.name: t for t in tools}
        assert by_name["search"].read_only_hint is True
        assert by_name["search"].destructive_hint is False
        assert by_name["search"].idempotent_hint is True
        assert by_name["plain"].read_only_hint is None
        await client.close()


class TestMCPToolBridge:
    def test_qualified_name_sanitises(self) -> None:
        from prodagent.mcp.bridge import qualified_name

        assert qualified_name("rca", "search") == "mcp__rca__search"
        assert qualified_name("my.svc-1", "tool") == "mcp__my_svc-1__tool"
        assert qualified_name("weird.name!", "tool@x") == "mcp__weird_name___tool_x"

    async def test_bridge_name_is_qualified_schema_uses_qualified(self) -> None:
        cfg = MCPServerConfig(name="rca", transport="http", url="http://x/mcp")
        client = MCPClient(cfg)
        _patch_client_session(
            client,
            _mock_server_handler(
                tools=[{"name": "search", "description": "d", "inputSchema": {"type": "object"}}],
            ),
        )
        tools = await adapt_mcp_tools(client, readonly_patterns=["search"])
        assert len(tools) == 1
        t = tools[0]
        assert t.name == "mcp__rca__search"
        assert t.schema["name"] == "mcp__rca__search"
        assert t.meta.is_readonly is True
        assert t.meta.side_effect_level.value == "low"
        await client.close()

    async def test_read_only_hint_overrides_side_effect_level(self) -> None:
        cfg = MCPServerConfig(name="rca", transport="http", url="http://x/mcp")
        client = MCPClient(cfg)
        _patch_client_session(
            client,
            _mock_server_handler(
                tools=[
                    {
                        "name": "fetch",
                        "description": "d",
                        "inputSchema": {},
                        "annotations": {"readOnlyHint": True},
                    }
                ],
            ),
        )
        tools = await adapt_mcp_tools(client)
        t = tools[0]
        assert t.meta.is_readonly is True
        assert t.meta.side_effect_level.value == "low"
        await client.close()

    async def test_destructive_hint_marks_high_risk_and_requires_approval(self) -> None:
        cfg = MCPServerConfig(name="rca", transport="http", url="http://x/mcp")
        client = MCPClient(cfg)
        _patch_client_session(
            client,
            _mock_server_handler(
                tools=[
                    {
                        "name": "delete_repo",
                        "description": "d",
                        "inputSchema": {},
                        "annotations": {"destructiveHint": True},
                    }
                ],
            ),
        )
        tools = await adapt_mcp_tools(client)
        t = tools[0]
        assert t.meta.is_readonly is False
        assert t.meta.side_effect_level.value == "high"
        await client.close()

    async def test_readonly_pattern_still_falls_back_without_hints(self) -> None:
        cfg = MCPServerConfig(name="rca", transport="http", url="http://x/mcp")
        client = MCPClient(cfg)
        _patch_client_session(
            client,
            _mock_server_handler(
                tools=[{"name": "search", "description": "d", "inputSchema": {}}],
            ),
        )
        tools = await adapt_mcp_tools(client, readonly_patterns=["search"])
        t = tools[0]
        assert t.meta.is_readonly is True
        assert t.meta.side_effect_level.value == "low"
        await client.close()

    async def test_bridge_call_forwards_raw_name(self) -> None:
        cfg = MCPServerConfig(name="rca", transport="http", url="http://x/mcp")
        client = MCPClient(cfg)
        received: dict[str, Any] = {}

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
                    {
                        "tools": [
                            {"name": "search", "description": "d", "inputSchema": {}},
                        ]
                    },
                )
            if method == "tools/call":
                received["name"] = body["params"]["name"]
                received["args"] = body["params"].get("arguments", {})
                return _sse_response(
                    req_id,
                    {
                        "content": [{"type": "text", "text": "ok"}],
                    },
                )
            return _json_response(req_id, {})

        _patch_client_session(client, handler)
        tools = await adapt_mcp_tools(client)
        t = tools[0]
        await t(query="hello")
        assert received["name"] == "search"
        assert received["args"] == {"query": "hello"}
        await client.close()


def _error_response(req_id: int | str, code: int, message: str) -> httpx.Response:
    return httpx.Response(
        200,
        headers={"content-type": "application/json"},
        content=json.dumps(
            {"jsonrpc": "2.0", "id": req_id, "error": {"code": code, "message": message}}
        ).encode(),
    )


class TestMCPErrorTranslation:
    async def _tool_returning_rpc_error(self, code: int, message: str = "boom") -> FunctionTool:

        cfg = MCPServerConfig(name="rca", transport="http", url="http://x/mcp")
        client = MCPClient(cfg)

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
                return _error_response(req_id, code, message)
            return _json_response(req_id, {})

        _patch_client_session(client, handler)
        tools = await adapt_mcp_tools(client)
        return tools[0]

    async def test_method_not_found_maps_to_red(self) -> None:
        t = await self._tool_returning_rpc_error(-32601, "method not found")
        result = await t(query="x")
        assert result.to_wire()["error_severity"] == "red"
        assert result.to_wire()["code"] == "mcp_method_error"

    async def test_invalid_params_maps_to_red(self) -> None:
        t = await self._tool_returning_rpc_error(-32602, "invalid params")
        result = await t(query="x")
        assert result.to_wire()["error_severity"] == "red"
        assert result.to_wire()["code"] == "mcp_method_error"

    async def test_server_error_maps_to_yellow(self) -> None:
        t = await self._tool_returning_rpc_error(-32000, "server down")
        result = await t(query="x")
        assert result.to_wire()["error_severity"] == "yellow"
        assert result.to_wire()["code"] == "mcp_server_error"

    async def test_internal_error_maps_to_yellow(self) -> None:
        t = await self._tool_returning_rpc_error(-32603, "internal")
        result = await t(query="x")
        assert result.to_wire()["error_severity"] == "yellow"

    async def test_non_rpc_runtime_error_maps_to_yellow(self) -> None:
        from prodagent.mcp.bridge import _translate_mcp_error

        err = _translate_mcp_error(RuntimeError("connection reset"), tool_name="mcp__rca__search")
        assert err.error_severity.value == "yellow"
        assert err.code == "mcp_transport_error"


class TestMCPRegistry:
    async def test_failure_isolation(self) -> None:
        good_cfg = MCPServerConfig(name="good", transport="http", url="http://good/mcp")
        bad_cfg = MCPServerConfig(name="bad", transport="http", url="http://bad/mcp")

        good_client = MCPClient(good_cfg)
        _patch_client_session(
            good_client,
            _mock_server_handler(
                tools=[{"name": "ping", "description": "d", "inputSchema": {}}],
            ),
        )

        bad_client = MCPClient(bad_cfg)

        async def bad_handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(500, content=b"down")

        bad_client._transport._session = _make_http_client(bad_handler)  # type: ignore[attr-defined]
        bad_client._transport._url = bad_cfg.url  # type: ignore[attr-defined]

        reg = MCPRegistry([good_cfg, bad_cfg])
        reg._clients = {}

        import asyncio as _aio

        async def _connect_one(c: MCPServerConfig) -> tuple[str, MCPClient | None, str | None]:
            if c.name == "good":
                return c.name, good_client, None
            try:
                await bad_client.connect()
                return c.name, bad_client, None
            except Exception as exc:
                return c.name, None, str(exc)

        results = await _aio.gather(*(_connect_one(c) for c in [good_cfg, bad_cfg]))
        for name, cl, err in results:
            if cl is not None:
                reg._clients[name] = cl
            else:
                reg._failures[name] = err or "unknown"

        assert "good" in reg.server_names
        assert "bad" not in reg.server_names
        assert "bad" in reg.failures

        tools = await reg.get_tools()
        assert len(tools) == 1
        assert tools[0].name == "mcp__good__ping"
        await reg.close_all()

    async def test_aggregates_tools_across_servers(self) -> None:
        cfg_a = MCPServerConfig(name="a", transport="http", url="http://a/mcp")
        cfg_b = MCPServerConfig(name="b", transport="http", url="http://b/mcp")

        client_a = MCPClient(cfg_a)
        client_b = MCPClient(cfg_b)
        _patch_client_session(
            client_a,
            _mock_server_handler(tools=[{"name": "search", "description": "d", "inputSchema": {}}]),
        )
        _patch_client_session(
            client_b,
            _mock_server_handler(tools=[{"name": "search", "description": "d", "inputSchema": {}}]),
        )

        reg = MCPRegistry([cfg_a, cfg_b])
        reg._clients = {"a": client_a, "b": client_b}

        tools = await reg.get_tools()
        names = {t.name for t in tools}
        assert names == {"mcp__a__search", "mcp__b__search"}
        await reg.close_all()


class TestEnvExpansionIntegration:
    async def test_config_file_env_expansion(
        self, tmp_path: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("MCP_HOST", "example.com")
        cfg_file = tmp_path / "mcp.json"
        cfg_file.write_text(
            json.dumps(
                {
                    "mcpServers": {
                        "api": {"type": "http", "url": "https://${MCP_HOST}/mcp"},
                    }
                }
            )
        )
        configs = load_mcp_servers(str(cfg_file))
        assert configs[0].url == "https://example.com/mcp"


class TestAgentMcpIntegration:
    def test_mcp_normalises_dict_configs(self, fake_llm: Any, hook_registry: Any) -> None:
        from prodagent import Agent, AgentConfig

        cfg = MCPServerConfig(name="rca", transport="http", url="http://x/mcp")
        agent = Agent(
            name="t",
            config=AgentConfig(name="t", llm=fake_llm, hooks=hook_registry, mcp=[cfg]),
        )
        assert len(agent.mcp_configs) == 1
        assert isinstance(agent.mcp_configs[0], MCPServerConfig)
        assert agent.mcp_configs[0].name == "rca"

    def test_mcp_rejects_dict_without_name(self, fake_llm: Any, hook_registry: Any) -> None:
        with pytest.raises(TypeError, match="required"):
            MCPServerConfig(transport="http", url="http://x")

    async def test_mcp_tools_injected_into_agent(self, fake_llm: Any, hook_registry: Any) -> None:
        from prodagent import Agent, AgentConfig
        from prodagent.mcp.registry import MCPRegistry

        cfg = MCPServerConfig(name="mock", transport="http", url="http://x/mcp")
        client = MCPClient(cfg)
        _patch_client_session(
            client,
            _mock_server_handler(
                tools=[{"name": "ping", "description": "d", "inputSchema": {}}],
            ),
        )

        agent = Agent(
            name="t",
            system_prompt="g",
            config=AgentConfig(name="t", llm=fake_llm, hooks=hook_registry, mcp=[cfg]),
        )

        agent.mcp_registry = MCPRegistry([cfg])
        assert agent.mcp_registry is not None
        agent.mcp_registry._clients = {"mock": client}
        tools = await agent.mcp_registry.get_tools()
        agent.inline_tools = [*agent.inline_tools, *tools]
        assert any(t.name == "mcp__mock__ping" for t in agent.inline_tools)
        await agent.mcp_registry.close_all()
