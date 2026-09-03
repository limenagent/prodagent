"""Workflow — a hand-written plan, declared in code and compiled to a ``Plan``.

A Workflow is a *declaration*, not a wiring: it names nodes, their bodies
(fn / tool / llm / agent), and their dependencies, and holds the plain
functions its fn nodes invoke — nothing else. No LLM client, no hooks, no
tool registration: those belong to the composition root, which injects
them at execution time. That is what makes a compiled Workflow reusable
across runs and harmless to share.

``compile()`` freezes the declaration into the same immutable ``Plan`` a
model could have drafted; a PLAN_FIRST agent executes it. The model never
plans — for compliance pipelines the path is fixed by design, and "which
node runs next" is auditable from source alone. Workflow steps do not
enter the agent's tool table: the graph is for the executor to see, not
the model.
"""

from __future__ import annotations

import inspect
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from prodagent.kernel.graph import Node, Origin
from prodagent.kernel.units import FnUnit, LLMUnit, SubAgentUnit, ToolUnit

if TYPE_CHECKING:
    from collections.abc import Callable

    from prodagent.kernel.graph import Plan
    from prodagent.llm import LLMConfig
    from prodagent.runtime.agent import Agent

__all__ = ["Workflow"]


@dataclass
class _NodeSpec:
    """Deferred description of one plan node, resolved at compile time."""

    node_id: str
    body: FnUnit | ToolUnit | LLMUnit | SubAgentUnit
    params: dict[str, Any] = field(default_factory=dict)
    depends_on: list[str] = field(default_factory=list)
    is_terminal: bool = False


class Workflow:
    """A plan *builder*, not a third execution mode."""

    def __init__(self) -> None:
        self._specs: list[_NodeSpec] = []
        self._fns: dict[str, Callable[..., Any]] = {}

    @property
    def fns(self) -> dict[str, Callable[..., Any]]:
        """The plain functions fn nodes invoke, by name — handed to the
        composition root so the scheduler's UnitContext can resolve them at execution."""
        return dict(self._fns)

    def step(
        self,
        fn: Callable[..., Any] | Agent | None = None,
        *,
        name: str | None = None,
        depends_on: list[str] | None = None,
        is_terminal: bool = False,
        params: dict[str, Any] | None = None,
    ) -> Any:
        """Register a plain-function step (usable as a decorator), or an
        Agent as a sub-task (compiled to a ``spawn_agent`` body). Function
        parameters whose names match dependencies auto-bind to that node's
        output — the wiring reads like ordinary function calls."""
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

        node_id = name or fn.__name__
        self._fns[node_id] = fn
        bound = self._auto_bind_params(fn, depends_on or [])
        if params:
            bound = {**bound, **params}
        self._specs.append(
            _NodeSpec(
                node_id=node_id,
                body=FnUnit(fn=node_id),
                params=bound,
                depends_on=list(depends_on or []),
                is_terminal=is_terminal,
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
        """Register a model step: the LLM answers *this step's* prompt and
        nothing else — it processes input, it never decides flow. The
        declared prompt may be overridden per-run by a ``prompt`` param
        bound to an upstream output (``{{dep.output}}``). ``config`` /
        ``timeout_ms`` are accepted for call-compat and ignored: the body
        is declarative, the composition root owns clients and deadlines."""
        bound: dict[str, Any] = {"prompt": prompt}
        if params:
            bound = {**bound, **params}
        self._specs.append(
            _NodeSpec(
                node_id=name,
                body=LLMUnit(prompt=prompt, system=system or ""),
                params=bound,
                depends_on=list(depends_on or []),
                is_terminal=is_terminal,
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
        """Register a step that calls an already-registered tool by name —
        the plan references the live tool directly, through the same
        five-gate pipeline as any other governed call."""
        self._specs.append(
            _NodeSpec(
                node_id=name,
                body=ToolUnit(tool=tool_name),
                params=params or {},
                depends_on=list(depends_on or []),
                is_terminal=is_terminal,
            )
        )

    def node_declarations(self) -> list[Node]:
        """The compiled node set, before instantiation — the WorkflowTemplate
        half of the compiler's front-end."""
        return [
            Node(
                node_id=s.node_id,
                body=s.body,
                params=s.params,
                depends_on=list(s.depends_on),
                is_terminal=s.is_terminal,
                origin=Origin.STATIC,
            )
            for s in self._specs
        ]

    def compile(self) -> Plan:
        """Freeze the declaration into a ``Plan`` through the IR and the
        five-check validator — a hand-written cycle or dangling edge fails
        loudly HERE, not as a hang at run."""
        from prodagent.plan.ir.compiler import compile_workflow

        return compile_workflow(
            self, fn_sigs={name: inspect.signature(fn) for name, fn in self._fns.items()}
        )

    def _register_agent_step(
        self,
        agent: Agent,
        *,
        name: str | None,
        depends_on: list[str] | None,
        is_terminal: bool,
        params: dict[str, Any] | None,
    ) -> None:
        node_id = name or agent.name
        merged: dict[str, Any] = {"task": f"Execute {node_id}"}
        if params:
            merged.update(params)
        self._specs.append(
            _NodeSpec(
                node_id=node_id,
                body=SubAgentUnit(agent=agent.name, task=f"Execute {node_id}"),
                params=merged,
                depends_on=list(depends_on or []),
                is_terminal=is_terminal,
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
                # Name match = data-flow edge: the parameter binds to that
                # dependency's output, declared without any wiring syntax.
                params[pname] = f"{{{{{pname}.output}}}}"
        return params
