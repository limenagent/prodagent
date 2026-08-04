from __future__ import annotations

import inspect
from typing import TYPE_CHECKING, Any, Self

from prodagent.core.budget import HardBudget
from prodagent.core.types import ExecutionMode
from prodagent.hooks.checkpoint import CheckPoint, InjectionPoint
from prodagent.hooks.events import HookEvent
from prodagent.mcp.config import MCPServerConfig

if TYPE_CHECKING:
    from collections.abc import Callable

    from prodagent.hooks.registry import HookRegistry
    from prodagent.runtime.agent import Agent


def _make_injector(f: Callable[..., Any]) -> Callable[..., Any]:
    sig = inspect.signature(f)
    if len(sig.parameters) == 1:

        def _injector(**kw: Any) -> Any:
            return f(kw.get("query", ""))
    else:

        def _injector(**kw: Any) -> Any:
            return f(kw)

    return _injector


class AgentFluentMixin:
    config: Any  # AgentConfig — set by Agent.__init__
    _fluent_wired: bool = False  # set by Agent.__init__; annotated here for mypy

    @property
    def hooks(self) -> HookRegistry | None: ...

    @property
    def framework_config(self) -> Any: ...

    def inject(self, point_or_fn: Any, fn: Callable[..., Any] | None = None) -> Self:
        if fn is None:
            point, handler = InjectionPoint.CONTEXT_INJECTOR, point_or_fn
        else:
            point, handler = point_or_fn, fn
        self.config.injectors.append((point, handler))
        return self

    def check(self, point: CheckPoint, fn: Callable[..., Any]) -> Self:
        self.config.checkers.append((point, fn))
        return self

    def extend(self, *exts: Any) -> Self:
        self.config.extensions.extend(exts)
        return self

    def on(self, event: HookEvent, fn: Callable[..., Any]) -> Self:
        self.config.event_handlers.append((event, fn))
        return self

    def mcp(self, configs: list[MCPServerConfig | dict[str, Any]]) -> Self:
        normalized: list[MCPServerConfig] = []
        for c in configs:
            if isinstance(c, MCPServerConfig):
                normalized.append(c)
            elif isinstance(c, dict):
                name = c.get("name")
                if not name:
                    raise ValueError("MCP config dict must include a 'name' field")
                normalized.append(MCPServerConfig.from_dict(name, c))
            else:
                raise TypeError(f".mcp() expects MCPServerConfig or dict, got {type(c).__name__}")
        self.config.mcp_configs = normalized
        return self

    def description(self, description: str) -> Self:
        self.config.description = description
        return self

    def agents(self, child_agents: list[Agent]) -> Self:
        self.config.child_agents = list(child_agents)
        return self

    def peers(self, peer_agents: list[Agent]) -> Self:
        self.config.peer_agents = list(peer_agents)
        return self

    def reactive(self) -> Self:
        self.config.mode = ExecutionMode.REACTIVE
        return self

    def workflow(self, wf: Any, *, allow_replan: bool = True) -> Self:
        from prodagent.runtime.workflow import Workflow

        if not isinstance(wf, Workflow):
            raise TypeError(f".workflow() expects a Workflow, got {type(wf).__name__}")
        llm = self.config.llm
        if llm is None:
            from prodagent.backends.factory import resolve_llm

            llm = resolve_llm(self.framework_config)
        wf.bind(llm, self.hooks)
        self.config.mode = ExecutionMode.PLAN_FIRST
        self.config.initial_plan = wf.compile()
        self.config.max_replans = 0 if not allow_replan else self.config.max_replans
        self.config.tools = [*self.config.tools, *wf.tools]
        return self

    def plan_first(self) -> Self:
        self.config.mode = ExecutionMode.PLAN_FIRST
        return self

    def budget(
        self,
        *,
        turns: int | None = None,
        cost_usd: float | None = None,
        seconds: float = 300.0,
        tokens: int | None = None,
    ) -> Self:
        kwargs: dict[str, Any] = {"max_seconds": seconds}
        if turns is not None:
            kwargs["max_turns"] = turns
        if cost_usd is not None:
            kwargs["max_cost_usd"] = cost_usd
        if tokens is not None:
            kwargs["max_tokens"] = tokens
        self.config.budget = HardBudget(**kwargs)
        return self

    def wire_fluent_hooks(self, hooks: HookRegistry) -> None:
        if self._fluent_wired:
            return
        self._fluent_wired = True

        for point, fn in self.config.injectors:
            if not isinstance(point, InjectionPoint):
                raise TypeError(
                    f"inject() point must be an InjectionPoint member, "
                    f"got {type(point).__name__}: {point!r}"
                )
            hooks.register_injector(point, _make_injector(fn))

        for point, fn in self.config.checkers:
            if not isinstance(point, CheckPoint):
                raise TypeError(
                    f"check() point must be a CheckPoint member, "
                    f"got {type(point).__name__}: {point!r}"
                )
            hooks.register_checker(point, fn)

        for event_name, fn in self.config.event_handlers:
            if not isinstance(event_name, HookEvent):
                raise TypeError(
                    f"on() event must be a HookEvent member, "
                    f"got {type(event_name).__name__}: {event_name!r}"
                )
            hooks.register_event(event_name, fn)

        for ext in self.config.extensions:
            hooks.attach_extension(ext)

    @property
    def injectors(self) -> list[tuple[InjectionPoint, Callable[..., Any]]]:
        return list(self.config.injectors)
