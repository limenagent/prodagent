"""Workflow — hand-written deterministic plans compiled to ``Plan`` + tools."""

from __future__ import annotations

import inspect
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from prodagent.core.types import SideEffectLevel, ToolMeta
from prodagent.plan.dag import Plan, PlanStep
from prodagent.tooling.base import FunctionTool
from prodagent.tooling.decorator import _infer_schema, tool

if TYPE_CHECKING:
    from collections.abc import Callable

    from prodagent.hooks.registry import HookRegistry
    from prodagent.llm import LLMConfig
    from prodagent.ports.llm import LLMClient
    from prodagent.runtime.agent import Agent

__all__ = ["Workflow"]

_LLM_STEP_TIMEOUT_MARGIN_MS = 90_000


@dataclass
class _StepSpec:
    """Deferred description of a plan step, resolved at compile time."""

    step_id: str
    action: str
    params: dict[str, Any] = field(default_factory=dict)
    depends_on: list[str] = field(default_factory=list)
    is_terminal: bool = False
    tool: FunctionTool | None = None


class Workflow:
    def __init__(self) -> None:
        self._specs: list[_StepSpec] = []
        self._llm: LLMClient | None = None
        self._hooks: HookRegistry | None = None

    def bind(self, llm: LLMClient | None, hooks: HookRegistry | None) -> None:
        # llm_step tool closures capture this instance at authoring time, so a
        # Workflow is bound exactly once. Re-binding a *different* client would
        # silently rewire every plan compiled from it — refuse loudly instead.
        if self._llm is not None and llm is not self._llm:
            raise ValueError(
                "This Workflow is already bound to another LLM client. A Workflow "
                "instance owns one binding (its llm_step closures capture it); build "
                "a fresh Workflow per Agent."
            )
        self._llm = llm
        self._hooks = hooks

    def step(
        self,
        fn: Callable[..., Any] | Agent | None = None,
        *,
        name: str | None = None,
        depends_on: list[str] | None = None,
        is_terminal: bool = False,
        params: dict[str, Any] | None = None,
    ) -> Any:
        if fn is None:

            def _decorator(f: Callable[..., Any] | Agent) -> Any:
                return self.step(
                    f,
                    name=name,
                    depends_on=depends_on,
                    is_terminal=is_terminal,
                    params=params,
                )

            return _decorator

        from prodagent.runtime.agent import Agent

        if isinstance(fn, Agent):
            self._register_agent_step(
                fn,
                name=name,
                depends_on=depends_on,
                is_terminal=is_terminal,
                params=params,
            )
            return fn

        step_id = name or fn.__name__
        tool_obj = self._wrap_function(fn, step_id)
        bound = self._auto_bind_params(fn, depends_on or [])
        if params:
            bound = {**bound, **params}
        self._specs.append(
            _StepSpec(
                step_id=step_id,
                action=tool_obj.name,
                params=bound,
                depends_on=list(depends_on or []),
                is_terminal=is_terminal,
                tool=tool_obj,
            )
        )
        return fn

    def llm_step(
        self,
        name: str,
        prompt: str,
        *,
        system: str | None = None,
        depends_on: list[str] | None = None,
        is_terminal: bool = False,
        params: dict[str, Any] | None = None,
        config: LLMConfig | None = None,
        timeout_ms: float | None = None,
    ) -> None:
        tool_obj = self._make_llm_tool(
            name,
            prompt,
            system=system,
            config=config,
            timeout_ms=timeout_ms,
        )
        bound: dict[str, Any] = {"prompt": prompt}
        if params:
            bound = {**bound, **params}
        self._specs.append(
            _StepSpec(
                step_id=name,
                action=tool_obj.name,
                params=bound,
                depends_on=list(depends_on or []),
                is_terminal=is_terminal,
                tool=tool_obj,
            )
        )

    def tool_step(
        self,
        name: str,
        tool_name: str,
        *,
        params: dict[str, Any] | None = None,
        depends_on: list[str] | None = None,
        is_terminal: bool = False,
    ) -> None:
        self._specs.append(
            _StepSpec(
                step_id=name,
                action=tool_name,
                params=params or {},
                depends_on=list(depends_on or []),
                is_terminal=is_terminal,
                tool=None,
            )
        )

    def compile(self) -> Plan:
        plan = Plan()
        steps = [
            PlanStep(
                step_id=s.step_id,
                action=s.action,
                params=s.params,
                depends_on=list(s.depends_on),
                is_terminal=s.is_terminal,
            )
            for s in self._specs
        ]
        plan.add_steps(steps)
        return plan

    @property
    def tools(self) -> list[FunctionTool]:
        return [s.tool for s in self._specs if s.tool is not None]

    def _wrap_function(self, fn: Callable[..., Any], step_id: str) -> FunctionTool:
        return tool(fn, name=step_id)

    def _make_llm_tool(
        self,
        name: str,
        prompt: str,
        *,
        system: str | None,
        config: LLMConfig | None,
        timeout_ms: float | None,
    ) -> FunctionTool:
        from prodagent.llm import LLMConfig as _LLMConfig

        wf = self
        llm_config = config or _LLMConfig()
        resolved_timeout_ms = (
            timeout_ms
            if timeout_ms is not None
            else llm_config.timeout_seconds * 1_000 + _LLM_STEP_TIMEOUT_MARGIN_MS
        )

        async def _llm_fn(prompt: str, run_id: str = "") -> str:
            if wf._llm is None:
                raise RuntimeError(
                    f"llm_step {name!r}: LLM client not bound. Pass workflow=wf to Agent() "
                    "to bind the agent's LLM before running."
                )
            from prodagent.hooks import fire as _fire
            from prodagent.hooks.events import HookEvent
            from prodagent.llm import noop_chunk

            sys_text = system or ""
            await _fire(
                wf._hooks,
                HookEvent.LLM_REQUEST,
                system=sys_text[:200],
                system_len=len(sys_text),
                messages=[{"role": "user", "content": prompt}],
                msg_count=1,
                phase="workflow",
                run_id=run_id,
            )
            response = await wf._llm.complete(
                [{"role": "user", "content": prompt}],
                system=sys_text,
                config=llm_config,
                on_chunk=noop_chunk,
            )
            return response.content or ""

        schema = _infer_schema(_llm_fn, name, f"LLM step: {prompt[:120]}")
        meta = ToolMeta(
            name=name,
            is_readonly=True,
            side_effect_level=SideEffectLevel.LOW,
            timeout_seconds=resolved_timeout_ms / 1_000,
        )
        fn_tool = FunctionTool(name=name, fn=_llm_fn, meta=meta, schema=schema, inject_run_id=True)
        return fn_tool

    def _register_agent_step(
        self,
        agent: Agent,
        *,
        name: str | None,
        depends_on: list[str] | None,
        is_terminal: bool,
        params: dict[str, Any] | None,
    ) -> None:
        step_id = name or agent.name
        merged: dict[str, Any] = {"name": agent.name, "task": f"Execute {step_id}"}
        if params:
            merged.update(params)
            merged["name"] = agent.name
        self._specs.append(
            _StepSpec(
                step_id=step_id,
                action="spawn_agent",
                params=merged,
                depends_on=list(depends_on or []),
                is_terminal=is_terminal,
                tool=None,
            )
        )

    @staticmethod
    def _auto_bind_params(fn: Callable[..., Any], depends_on: list[str]) -> dict[str, Any]:
        """Bind any parameter whose name matches a dependency to {{dep.output}}."""
        sig = inspect.signature(fn)
        deps = set(depends_on)
        params: dict[str, Any] = {}
        for pname, param in sig.parameters.items():
            if pname in ("self",) or param.kind in (
                inspect.Parameter.VAR_KEYWORD,
                inspect.Parameter.VAR_POSITIONAL,
            ):
                continue
            if pname in deps:
                params[pname] = f"{{{{{pname}.output}}}}"
        return params
