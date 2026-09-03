"""Built-in Units — the five things a graph node's execution can be.

A node says *what to run and what it waits for*; its unit says *how one
execution goes*: a pure function (FnUnit), a governed tool call (ToolUnit),
one fixed-prompt model call (LLMUnit), an autonomous think-act loop
(AutonomousUnit), or a child agent (SubAgentUnit). The scheduler never branches
on these — it calls ``unit.run(input, ctx)`` and takes the
:class:`~prodagent.kernel.unit.Outcome` — which is what keeps one engine
serving every autonomy level.

Units stay declarative, frozen, and serializable: they carry *names and
prompts*, never clients or callables. Execution collaborators arrive per
execution on the :class:`~prodagent.kernel.unit.UnitContext` (the tool
throat, the fn table, the model invoker, the loop engine, the activation
slot) — the composition root fills the context, the unit draws on it. The
durable wire form is kind plus target name plus extras
(:func:`unit_to_wire_extras` / :func:`unit_from_wire`), so a checkpoint
reconstructs any unit without ever holding a live object.
"""

from __future__ import annotations

import inspect
from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING, Any

from prodagent.kernel.unit import Outcome, UnitContext

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator

    from prodagent.kernel.types import AgentEvent, ToolCall


class NodeKind(StrEnum):
    """The five unit kinds — column 3's node-autonomy ladder plus delegation."""

    FN = "fn"
    TOOL = "tool"
    LLM = "llm"
    AUTONOMOUS = "autonomous"
    SUBAGENT = "subagent"


@dataclass(frozen=True)
class FnUnit:
    """L0 — a pure Python function from the context's fn table.
    Deterministic, side-effect-free, zero model calls; exactly the function
    the Workflow author wrote, with no tool-registry costume."""

    kind = NodeKind.FN
    fn: str
    readonly = True

    @property
    def target(self) -> str:
        return self.fn

    async def run(self, input: ToolCall, ctx: UnitContext) -> Outcome:
        fn = ctx.fns.get(self.fn)
        if fn is None:
            raise KeyError(
                f"fn node {self.fn!r}: no function registered under this name. "
                "Workflow step functions are registered at compile time — "
                "was this plan compiled by a different Workflow?"
            )
        value = fn(**input.params)
        return Outcome(value=await value if inspect.isawaitable(value) else value)


@dataclass(frozen=True)
class ToolUnit:
    """L1 — one governed tool call by name. Params are resolved from the
    graph before execution; the call then funnels into the same dispatcher
    pipeline (approval gate, hooks, breaker, spill) as a REACTIVE turn's
    tools — one throat, identical policy."""

    kind = NodeKind.TOOL
    tool: str
    readonly = None  # ask the tool registry's metadata

    @property
    def target(self) -> str:
        return self.tool

    async def run(self, input: ToolCall, ctx: UnitContext) -> Outcome:
        if ctx.tools is None:
            raise RuntimeError(
                f"tool node {self.tool!r}: no tool executor on this context. "
                "Governed calls need the dispatcher wired at composition time."
            )
        return Outcome(value=await ctx.tools(input, run_id=ctx.run_id))


@dataclass(frozen=True)
class LLMUnit:
    """L2 — exactly one model call with a fixed prompt. It *processes* input,
    it never decides flow; the model's answer becomes the node's output and
    flows downhill like any other."""

    kind = NodeKind.LLM
    prompt: str
    system: str = ""
    readonly = True

    @property
    def target(self) -> str:
        return "llm"

    async def run(self, input: ToolCall, ctx: UnitContext) -> Outcome:
        if ctx.llm is None:
            raise RuntimeError(
                "llm node: no model invoker on this context. LLM units need "
                "an LLM client at composition time (Agent(llm=...) or "
                "framework defaults)."
            )
        # A resolved param may override the declared prompt — that is how
        # upstream output flows into a fixed-prompt step ({{dep.output}}
        # bound to "prompt"), same precedence the old tool wrapper had.
        prompt = str(input.params.get("prompt") or self.prompt)
        return Outcome(value=await ctx.llm(prompt, system=self.system, run_id=ctx.run_id))


@dataclass(frozen=True)
class AutonomousUnit:
    """L3 — an autonomous think-act loop: Turns of model calls and tool
    batches until the model says done. The loop lives *inside* the unit;
    from the scheduler's view this is still just one node.

    With a ``goal`` this is the coarse-planning school's autonomous node
    (column 7): the planner declares WHAT to achieve, the unit works out
    HOW with the same tools — and its finish settles the node, not the run.

    Live Turns stream through the optional :class:`StreamingUnit` form
    (``run_stream`` — the driver yields them as they happen, inline on its
    own stack); the engine writes the run's transcript directly (the driver
    commits it unwrapped)."""

    kind = NodeKind.AUTONOMOUS
    goal: str = ""
    readonly = False

    @property
    def target(self) -> str:
        return "loop"

    def run_stream(
        self, input: ToolCall, ctx: UnitContext, box: list[Outcome]
    ) -> AsyncGenerator[AgentEvent, None]:
        """The loop's native form: Turns as they happen, one Outcome boxed."""
        if ctx.engine is None:
            raise RuntimeError(
                "autonomous node: no engine on this context. Autonomous "
                "units need the Turn loop's collaborators at composition time."
            )
        if ctx.run is None:
            raise RuntimeError("autonomous node: no live run on this context to drive.")
        return self._drive(ctx, box)

    async def _drive(
        self, ctx: UnitContext, box: list[Outcome]
    ) -> AsyncGenerator[AgentEvent, None]:
        assert ctx.engine is not None and ctx.run is not None  # guarded by run_stream
        if self.goal:
            # Coarse-planning autonomous node: the goal scopes the loop, and
            # its finish settles this node, not the run.
            async for event in ctx.engine.drive(ctx.run, goal=self.goal, settle_run=False):
                yield event
            box.append(Outcome(value=ctx.engine.outcome_of(ctx.run, goal_scope=True)))
            return
        async for event in ctx.engine.drive(ctx.run):
            yield event
        box.append(Outcome(value=ctx.engine.outcome_of(ctx.run)))

    async def run(self, input: ToolCall, ctx: UnitContext) -> Outcome:
        """Draining form: same execution, events dropped on the floor."""
        box: list[Outcome] = []
        async for _ in self.run_stream(input, ctx, box):
            pass
        return box[0]


@dataclass(frozen=True)
class SubAgentUnit:
    """Delegation — activate a child agent and fold its final output into
    this node. The parent hands over a task and reads back a report; the
    child runs the same kernel recursively (column 26's Run tree)."""

    kind = NodeKind.SUBAGENT
    agent: str
    task: str = ""
    readonly = False

    @property
    def target(self) -> str:
        return self.agent

    async def run(self, input: ToolCall, ctx: UnitContext) -> Outcome:
        if ctx.subagent is None:
            raise RuntimeError(
                f"subagent node {self.agent!r}: no activation on this context. "
                "Delegation units need the activation port at composition time."
            )
        # The task may flow in from upstream (params resolve {{...}}); the
        # declared task is the floor, never the ceiling.
        task = str(input.params.get("task") or self.task or f"Execute {self.agent}")
        result = await ctx.subagent(self.agent, task, run_id=ctx.run_id)
        return Outcome(value=_fold_child(self.agent, result))


def _fold_child(agent: str, result: dict[str, Any]) -> Any:
    """Fold a child activation's terminal state into node-outcome data:
    completion carries the report, suspension parks on the child's approval
    id, failure becomes red feedback (replanning IS the recovery)."""
    from prodagent.kernel.types import ToolResult

    state = str(result.get("state", "failed"))
    if state == "completed":
        return result
    if state == "suspended":
        return ToolResult.suspended(
            reason=f"child {agent!r} awaiting approval",
            tool="subagent",
            approval_request_id=str(result.get("approval_request_id", "")),
        )
    from prodagent.base.errors import ErrorReason
    from prodagent.kernel.types import ToolError

    return ToolResult.from_error(
        ToolError.from_reason(
            ErrorReason.UNKNOWN,
            code="subagent_failed",
            message=str(result.get("output") or f"child {agent!r} ended {state}"),
        ),
        tool="subagent",
    )


NodeUnit = FnUnit | ToolUnit | LLMUnit | AutonomousUnit | SubAgentUnit
"""The unit union a Node carries and the scheduler runs."""


def unit_from_wire(kind: str, action: str, extra: dict[str, Any]) -> NodeUnit:
    """Rebuild a unit from its durable form — kind plus the target name,
    plus the prompt for LLM units. A missing kind reads as a tool unit:
    that is what every node was before kinds existed, and a checkpoint
    that predates the split should resume, not crash."""
    node_kind = NodeKind(kind) if kind else NodeKind.TOOL
    match node_kind:
        case NodeKind.FN:
            return FnUnit(fn=action)
        case NodeKind.LLM:
            return LLMUnit(prompt=extra.get("prompt", ""), system=extra.get("system", ""))
        case NodeKind.AUTONOMOUS:
            return AutonomousUnit(goal=extra.get("goal", ""))
        case NodeKind.SUBAGENT:
            return SubAgentUnit(agent=action, task=extra.get("task", ""))
        case _:
            return ToolUnit(tool=action)


def unit_to_wire_extras(unit: Any) -> dict[str, Any]:
    """Kind-specific fields beyond the target name (prompts and goals)."""
    if isinstance(unit, LLMUnit):
        return {"prompt": unit.prompt, "system": unit.system}
    if isinstance(unit, SubAgentUnit):
        return {"task": unit.task}
    if isinstance(unit, AutonomousUnit):
        return {"goal": unit.goal}
    return {}


__all__ = [
    "NodeKind",
    "FnUnit",
    "ToolUnit",
    "LLMUnit",
    "AutonomousUnit",
    "SubAgentUnit",
    "NodeUnit",
    "unit_from_wire",
    "unit_to_wire_extras",
]
