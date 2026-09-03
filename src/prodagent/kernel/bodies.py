"""Built-in bodies — the four things a graph node's execution can be.

A node says *what to run and what it waits for*; its body says *how one
execution goes*: a pure function (FnBody), a governed tool call (ToolBody),
one fixed-prompt model call (LLMBody), or a child plan (SubPlanBody). An
autonomous think-act loop is none of these — it is a recipe body living
above the kernel (``runtime/recipes/loop_body``). The scheduler never
branches on these — it calls ``body.run(input, ctx)`` and takes the
:class:`~prodagent.kernel.body.Outcome` — which is what keeps one engine
serving every autonomy level.

Bodies stay declarative, frozen, and serializable: they carry *names and
prompts*, never clients or callables. Execution collaborators arrive per
execution on the :class:`~prodagent.kernel.body.NodeContext` (the tool
throat, the fn table, the model invoker, the loop engine, the activation
slot) — the composition root fills the context, the body draws on it. The
durable wire form is kind plus target name plus extras
(:func:`body_to_wire_extras` / :func:`body_from_wire`), so a checkpoint
reconstructs any body without ever holding a live object.
"""

from __future__ import annotations

import inspect
from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING, Any

from prodagent.kernel.body import NodeContext, Outcome

if TYPE_CHECKING:
    from prodagent.kernel.types import ToolCall


class NodeKind(StrEnum):
    """The body kinds the kernel knows — column 3's four. A think-act loop
    is NOT one of them (ReAct is a strategy, not a mechanism): the loop
    body lives in the recipes layer above, arriving as a composed body
    whose kind is the plain string ``"loop"``."""

    FN = "fn"
    TOOL = "tool"
    LLM = "llm"
    SUBAGENT = "subagent"


@dataclass(frozen=True)
class FnBody:
    """L0 — a pure Python function from the context's fn table.
    Deterministic, side-effect-free, zero model calls; exactly the function
    the Workflow author wrote, with no tool-registry costume."""

    kind = NodeKind.FN
    fn: str
    readonly = True

    @property
    def target(self) -> str:
        return self.fn

    async def run(self, input: ToolCall, ctx: NodeContext) -> Outcome:
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
class ToolBody:
    """L1 — one governed tool call by name. Params are resolved from the
    graph before execution; the call then funnels into the same dispatcher
    pipeline (approval gate, hooks, breaker, spill) as an agent round's
    tools — one throat, identical policy."""

    kind = NodeKind.TOOL
    tool: str
    readonly = None  # ask the tool registry's metadata

    @property
    def target(self) -> str:
        return self.tool

    async def run(self, input: ToolCall, ctx: NodeContext) -> Outcome:
        if ctx.tools is None:
            raise RuntimeError(
                f"tool node {self.tool!r}: no tool executor on this context. "
                "Governed calls need the dispatcher wired at composition time."
            )
        return Outcome(value=await ctx.tools(input, run_id=ctx.run_id))


@dataclass(frozen=True)
class LLMBody:
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

    async def run(self, input: ToolCall, ctx: NodeContext) -> Outcome:
        if ctx.llm is None:
            raise RuntimeError(
                "llm node: no model invoker on this context. LLM bodies need "
                "an LLM client at composition time (Agent(llm=...) or "
                "framework defaults)."
            )
        # A resolved param may override the declared prompt — that is how
        # upstream output flows into a fixed-prompt step ({{dep.output}}
        # bound to "prompt"), same precedence the old tool wrapper had.
        prompt = str(input.params.get("prompt") or self.prompt)
        return Outcome(value=await ctx.llm(prompt, system=self.system, run_id=ctx.run_id))


@dataclass(frozen=True)
class SubPlanBody:
    """Delegation — activate a child plan's agent and fold its final output
    into this node. The parent hands over a task and reads back a report;
    the child runs the same kernel recursively (column 26's Run tree)."""

    kind = NodeKind.SUBAGENT
    agent: str
    task: str = ""
    readonly = False

    @property
    def target(self) -> str:
        return self.agent

    async def run(self, input: ToolCall, ctx: NodeContext) -> Outcome:
        if ctx.subagent is None:
            raise RuntimeError(
                f"subagent node {self.agent!r}: no activation on this context. "
                "Delegation bodies need the activation port at composition time."
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


BuiltinBody = FnBody | ToolBody | LLMBody | SubPlanBody
"""The body union a Node carries and the scheduler runs."""


def body_from_wire(kind: str, action: str, extra: dict[str, Any]) -> BuiltinBody:
    """Rebuild a body from its durable form — kind plus the target name,
    plus the prompt for LLM bodies. A missing kind reads as a tool body:
    that is what every node was before kinds existed, and a checkpoint
    that predates the split should resume, not crash."""
    if kind == "autonomous":
        raise ValueError(
            "checkpoint node kind 'autonomous' predates the loop-body split: "
            "the loop moved to the recipes layer (runtime/recipes/loop_body) "
            "and old autonomous checkpoints have no restore path — rerun from "
            "the session's next turn instead of resuming this checkpoint"
        )
    try:
        node_kind = NodeKind(kind) if kind else NodeKind.TOOL
    except ValueError as exc:
        raise ValueError(
            f"checkpoint node kind {kind!r} is not a kernel built-in — "
            "composed bodies (loop, sequential, parallel, route) are "
            "process-local and re-declared in code, never restored from wire"
        ) from exc
    match node_kind:
        case NodeKind.FN:
            return FnBody(fn=action)
        case NodeKind.LLM:
            return LLMBody(prompt=extra.get("prompt", ""), system=extra.get("system", ""))
        case NodeKind.SUBAGENT:
            return SubPlanBody(agent=action, task=extra.get("task", ""))
        case _:
            return ToolBody(tool=action)


def body_to_wire_extras(body: Any) -> dict[str, Any]:
    """Kind-specific fields beyond the target name (prompts and goals)."""
    if isinstance(body, LLMBody):
        return {"prompt": body.prompt, "system": body.system}
    if isinstance(body, SubPlanBody):
        return {"task": body.task}
    return {}


__all__ = [
    "NodeKind",
    "FnBody",
    "ToolBody",
    "LLMBody",
    "SubPlanBody",
    "BuiltinBody",
    "body_from_wire",
    "body_to_wire_extras",
]
