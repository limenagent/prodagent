"""The registry — where tool visibility is budgeted (tiering + breaker)."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from prodagent.tooling.reliability.circuit_breaker import ToolCircuitBreaker
from prodagent.tooling.search import ToolSearchConfig, ToolSearchIndex, preset_procedural

if TYPE_CHECKING:
    from prodagent.kernel.types import ToolMeta, ToolName
    from prodagent.tooling.base import FunctionTool

logger = logging.getLogger(__name__)

_L3_MAX_CONTRIBUTION = 3
_L3_TRIGGER_THRESHOLD = 15


class ToolRegistry:
    """Tiered tool exposure: every tool is callable, few are visible.

    Tool schemas are prompt tokens — a model handed 500 schemas burns budget
    and accuracy before its first call. L1 (core) is always visible; L2
    (domain) mounts by role; L3 (cold) is invisible until a search surfaces
    it, and only when the visible set is small enough to afford the addition.
    """

    def __init__(
        self,
        failure_threshold: int = 3,
        recovery_timeout_seconds: float = 60.0,
        *,
        window_seconds: float = 300.0,
        search_config: ToolSearchConfig | None = None,
        max_visible_tools: int = 20,
    ) -> None:
        self._l1_core: list[FunctionTool] = []
        self._l2_domain: dict[str, list[FunctionTool]] = {}
        self._l3_cold: list[FunctionTool] = []
        self._all: dict[ToolName, FunctionTool] = {}
        self._search_config = search_config
        self._l3_index: ToolSearchIndex | None = None
        self._max_visible = max_visible_tools
        self._breaker = ToolCircuitBreaker(
            failure_threshold=failure_threshold,
            recovery_timeout_seconds=recovery_timeout_seconds,
            window_seconds=window_seconds,
        )

    def register(self, tool: FunctionTool, *, tier: str = "l2", role: str = "general") -> None:
        """File a tool under a tier (l1 core / l2 domain / l3 cold). Every
        tier feeds ``_all`` — callability is unconditional; only *visibility*
        is tiered. L3 registration invalidates the search index so it
        rebuilds with the newcomer."""
        self._all[tool.name] = tool
        if tier == "l1":
            self._l1_core.append(tool)
        elif tier == "l2":
            self._l2_domain.setdefault(role, []).append(tool)
        elif tier == "l3":
            self._l3_cold.append(tool)
            self._l3_index = None  # invalidate; rebuilt lazily on next search
        else:
            raise ValueError(f"Unknown tier {tier!r}. Choose l1, l2, or l3.")

    async def get_active_tools(
        self,
        role: str = "general",
        intent: str = "",
        force_l3_query: str | None = None,
    ) -> list[FunctionTool]:
        """Compute this hop's visible menu: L1 always, L2 by role, L3 only
        when a query surfaces it into a still-lean list — then breaker-filter
        (a tripped tool is callable but hidden until it recovers)."""
        active: list[FunctionTool] = []
        for t in self._l1_core:
            if await self._breaker.is_available(t.name):  # core tools hide too when tripped
                active.append(t)
        active_names = {t.name for t in active}

        for t in self._l2_domain.get(role, []):
            # name check first: a tool can sit in both L1 and a role's L2 —
            # first mount wins, no duplicates in the schema list.
            if t.name not in active_names and await self._breaker.is_available(t.name):
                active.append(t)
                active_names.add(t.name)

        query = force_l3_query or intent
        # Cold-tier search only fires when the visible set is still lean —
        # adding L3 hits to an already-crowded menu would spend the tokens the
        # tiering exists to save.
        if query and len(active) < _L3_TRIGGER_THRESHOLD and self._l3_cold:
            if self._l3_index is None:
                config = self._search_config or preset_procedural()
                self._l3_index = ToolSearchIndex(self._l3_cold, config=config)
            for t in self._l3_index.search(query, max_results=_L3_MAX_CONTRIBUTION):
                if t.name not in active_names and await self._breaker.is_available(t.name):
                    active.append(t)
                    active_names.add(t.name)

        if len(active) > self._max_visible:
            logger.warning(
                "Active tool count %d exceeds cap %d; trimming L3 hits first",
                len(active),
                self._max_visible,
            )
            active = active[: self._max_visible]

        return active

    async def record_success(self, name: ToolName) -> None:
        await self._breaker.record_success(name)

    async def record_failure(self, name: ToolName) -> None:
        await self._breaker.record_failure(name)

    async def is_available(self, name: ToolName) -> bool:
        return await self._breaker.is_available(name)

    async def try_acquire_probe(self, name: ToolName) -> bool:
        return await self._breaker.try_acquire_probe(name)

    async def release_probe(self, name: ToolName) -> None:
        await self._breaker.release_probe(name)

    def schemas_for(self, tools: list[FunctionTool]) -> list[dict[str, Any]]:
        return [t.schema for t in tools]

    def get_meta(self, name: ToolName) -> ToolMeta:
        return self._all[name].meta

    @property
    def names(self) -> list[ToolName]:
        return list(self._all.keys())

    def __contains__(self, name: ToolName) -> bool:
        return name in self._all
