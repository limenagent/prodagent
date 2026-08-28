from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, Any

from prodagent.kernel.types import SideEffectLevel
from prodagent.mcp.client import MCPClient

if TYPE_CHECKING:
    from prodagent.mcp.config import MCPServerConfig
    from prodagent.tooling.base import FunctionTool

logger = logging.getLogger(__name__)


class MCPRegistry:
    """Async context manager owning N MCP server connections."""

    def __init__(self, configs: list[MCPServerConfig]) -> None:
        self._configs = configs
        self._clients: dict[str, MCPClient] = {}
        self._failures: dict[str, str] = {}
        self._discovery_failures: dict[str, str] = {}

    @property
    def server_names(self) -> list[str]:
        return list(self._clients.keys())

    @property
    def failures(self) -> dict[str, str]:
        return dict(self._failures)

    @property
    def discovery_failures(self) -> dict[str, str]:
        return dict(self._discovery_failures)

    async def __aenter__(self) -> MCPRegistry:
        await self.connect_all()
        return self

    async def __aexit__(self, *_: Any) -> None:
        await self.close_all()

    async def connect_all(self) -> None:
        # One unreachable server degrades its own tools, not the whole
        # toolbox: connect concurrently, record failures, carry on.
        if not self._configs:
            return

        async def _one(cfg: MCPServerConfig) -> tuple[str, MCPClient | None, str | None]:
            client = MCPClient(cfg)
            try:
                await client.connect()
                return cfg.name, client, None
            except Exception as exc:
                logger.warning("MCP server %r failed to connect: %s", cfg.name, exc)
                return cfg.name, None, str(exc)

        results = await asyncio.gather(*(_one(c) for c in self._configs))
        for name, client, err in results:
            if client is not None:
                self._clients[name] = client
            else:
                self._failures[name] = err or "unknown error"

        logger.info(
            "MCPRegistry: %d/%d servers connected (%d failed)",
            len(self._clients),
            len(self._configs),
            len(self._failures),
        )

    async def close_all(self) -> None:
        if not self._clients:
            return
        await asyncio.gather(*(c.close() for c in self._clients.values()), return_exceptions=True)
        self._clients.clear()

    async def get_tools(self) -> list[FunctionTool]:
        from prodagent.mcp.bridge import adapt_mcp_tools

        tools: list[FunctionTool] = []
        for name, client in self._clients.items():
            try:
                server_tools = await adapt_mcp_tools(
                    client,
                    side_effect_level=SideEffectLevel.MEDIUM,
                    readonly_patterns=None,
                    max_concurrency=8,
                )
                tools.extend(server_tools)
            except Exception as exc:
                logger.warning("MCPRegistry: tool discovery failed for server %r: %s", name, exc)
                self._discovery_failures[name] = f"tool discovery failed: {exc}"
        return tools
