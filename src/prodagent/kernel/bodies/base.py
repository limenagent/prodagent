"""Node bodies — the five things a node's execution can be.

A :class:`~prodagent.plan.dag.Node` says *what to run and what it waits
for*; its body says *how one node executes*: a pure function (FnBody), a
governed tool call (ToolBody), one fixed-prompt model call (LLMBody), an
autonomous think-act loop (ReActBody), or a child agent (SubAgentBody).
The scheduler never branches on these — it calls the body and takes the
result — which is what keeps one engine serving every autonomy level
(column 3: the node layer is polymorphic, the scheduler is not).

Bodies are declarative, frozen, and serializable: they carry *names and
prompts*, never clients or callables. Execution collaborators (the tool
executor, the fn table, the LLM invoker) are injected into
:class:`~prodagent.kernel.bodies.runner.BodyRunner` at composition time —
the same port discipline the rest of the kernel follows, and what lets a
Workflow be a pure declaration that never holds an LLM client.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING, Any, Protocol

if TYPE_CHECKING:
    from prodagent.kernel.types import ToolCall, ToolResult


class NodeKind(StrEnum):
    """The five body kinds — column 3's node-autonomy ladder plus delegation."""

    FN = "fn"
    TOOL = "tool"
    LLM = "llm"
    REACT = "react"
    SUBAGENT = "subagent"


class ToolExecutor(Protocol):
    """Executes one governed tool call — the same shape as
    ``ToolDispatcher.dispatch`` so the five gates (validation, scheduling,
    permission, approval, execution) apply identically wherever a tool is
    invoked. ``run_id`` is required in practice; the default keeps
    hand-written executors in tests working."""

    async def __call__(self, call: ToolCall, *, run_id: str = "") -> ToolResult: ...


class LLMInvoker(Protocol):
    """One fixed-prompt model call, as seen from the kernel: text in, text
    out. The composition root decides which client, which config, and which
    hooks fire around it."""

    async def __call__(self, prompt: str, *, system: str, run_id: str = "") -> str: ...


class SubagentInvoker(Protocol):
    """One child-agent activation, as seen from the kernel: name and task in,
    a :class:`~prodagent.coordination.spawn.ChildResult`-shaped dict out.
    Parentage, depth, the chained ledger and the location the child runs at
    are all the composition root's business — this is the activation port
    (column 26) in its narrowest kernel-facing form."""

    async def __call__(self, agent: str, task: str, run_id: str = "") -> dict[str, Any]: ...


@dataclass(frozen=True)
class FnBody:
    """L0 — a pure Python function from the injected fn table. Deterministic,
    side-effect-free, zero model calls; exactly the function the Workflow
    author wrote, with no tool-registry costume."""

    kind = NodeKind.FN
    fn: str
    readonly = True

    @property
    def target(self) -> str:
        return self.fn


@dataclass(frozen=True)
class ToolBody:
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


@dataclass(frozen=True)
class ReActBody:
    """L3 — an autonomous think-act loop: Turns of model calls and tool
    batches until the model says done. The loop lives *inside* the body;
    from the scheduler's view this is still just one node.

    With a ``goal`` this is the coarse-planning school's autonomous node
    (column 7): the planner declares WHAT to achieve, the body works out
    HOW with the same tools — and its finish settles the node, not the run."""

    kind = NodeKind.REACT
    goal: str = ""
    readonly = False

    @property
    def target(self) -> str:
        return "react"


@dataclass(frozen=True)
class SubAgentBody:
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


NodeBody = FnBody | ToolBody | LLMBody | ReActBody | SubAgentBody
"""The body union a Node carries and a BodyRunner interprets."""


def body_from_wire(kind: str, action: str, extra: dict[str, Any]) -> NodeBody:
    """Rebuild a body from its durable form — kind plus the target name,
    plus the prompt for LLM bodies. A missing kind reads as a tool body:
    that is what every node was before bodies existed, and a checkpoint
    that predates the split should resume, not crash."""
    node_kind = NodeKind(kind) if kind else NodeKind.TOOL
    match node_kind:
        case NodeKind.FN:
            return FnBody(fn=action)
        case NodeKind.LLM:
            return LLMBody(prompt=extra.get("prompt", ""), system=extra.get("system", ""))
        case NodeKind.REACT:
            return ReActBody(goal=extra.get("goal", ""))
        case NodeKind.SUBAGENT:
            return SubAgentBody(agent=action, task=extra.get("task", ""))
        case _:
            return ToolBody(tool=action)


def body_to_wire_extras(body: NodeBody) -> dict[str, Any]:
    """Kind-specific fields beyond the target name (prompts and goals)."""
    if isinstance(body, LLMBody):
        return {"prompt": body.prompt, "system": body.system}
    if isinstance(body, SubAgentBody):
        return {"task": body.task}
    if isinstance(body, ReActBody):
        return {"goal": body.goal}
    return {}
