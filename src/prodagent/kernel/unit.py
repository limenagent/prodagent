"""Unit — the one composable interface: everything runnable is a Unit.

An Agent, a governed tool call, a fixed-prompt model call, an autonomous
think-act loop, a child activation, a whole subgraph — all of them are "a
thing you hand an input and get an :class:`Outcome` back from". One
interface replaces the parallel vocabularies that had grown up around
"callable things that do work" (the executor/invoker protocols, the body
dataclasses with their external dispatcher): there are no macro graph nodes
and micro agents, only Units nesting Units.

Two vocabularies live beside the interface:

- :class:`Outcome` — what a Unit's run produces: a value, a state delta to
  merge under the run's reducer rules, and an explicit control-flow choice.
- :class:`UnitContext` — the execution wiring a Unit may draw on: the tool
  throat, the model invoker, the activation slot, the Turn engine, the fn
  table, and a live-event tap. The context is *wiring*, not data: run-scoped
  data travels through ``input``/``Outcome.state_delta``, never through the
  context (that split is what keeps checkpoints clean — the context is never
  serialized, so it may hold engines and dispatchers).

Control is explicit and binary: :class:`Return` (call-return — the caller
keeps control; this is spawn's semantics) or :class:`Handoff` (control
really transfers and does not come back; this is peer's semantics). A
delegate that wants the caller to resume says ``Return``; one that wants to
take over the rest of the run says ``Handoff``. There is no third meaning
hiding in a return code.

Declarative-and-serializable discipline carries over unchanged from the
body layer: a *declaration* (names and prompts, the wire form) plus a
context of injected collaborators reconstructs any Unit; Units themselves
may hold live collaborators and are never serialized — their durable form
is name-plus-extras, resolved against the registry at bind time.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Literal, Protocol, runtime_checkable

from prodagent.base.errors import NON_RETRYABLE_REASONS, ErrorReason
from prodagent.kernel.types import (
    ErrorSeverity,
    ToolError,
    ToolName,
    ToolOutcome,
    ToolResult,
)

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator, Callable, Mapping

    from prodagent.kernel.run import Run
    from prodagent.kernel.types import AgentEvent, ToolCall


# ════════════ the collaborator slots ════════════
#
# These are not "things that run" and never a composition vocabulary — they
# are the *service slots* a UnitContext may carry. The composition root fills
# them; Units draw on them through the context. Keeping them as narrow
# Protocols here (rather than importing the concrete dispatcher or client)
# preserves the kernel's dependency rule: kernel imports no capability
# package.


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


@runtime_checkable
class AutonomyEngine(Protocol):
    """The kernel's contract with an agent's inner loop — what
    :class:`prodagent.kernel.units.AutonomousUnit` drives. The
    implementation (model calls, tool batches, dead-loop guard) is
    composition; the kernel never imports one.

    ``drive`` iterates the loop over a run (streaming its live events);
    ``outcome_of`` folds the run's terminal flag into node-outcome data;
    ``record_terminal`` / ``bind_clock`` are the Scheduler's replay hooks."""

    def drive(self, run: Run, *, goal: str | None = None, settle_run: bool = True) -> Any: ...

    def outcome_of(self, run: Run, *, goal_scope: bool = False) -> ToolResult: ...

    def record_terminal(self, run: Run, event_type: Any) -> Any: ...

    def bind_clock(self, clock: Any) -> None: ...


# ════════════ the interface ════════════


@runtime_checkable
class Unit(Protocol):
    """Anything that can be scheduled, composed, or delegated to.

    ``run`` is the whole contract: input in, :class:`Outcome` out. A unit
    with live events (a ReAct loop's Turns) may also expose the same
    execution as a generator via :class:`StreamingUnit` — drivers that
    stream prefer that form; ``ctx.fire`` is the model-agnostic fallback.
    """

    async def run(self, input: Any, ctx: UnitContext) -> Outcome:
        """Execute once. Never raises a *node* failure — a failed execution
        is ``Outcome`` data (the driver classifies); run-death exceptions
        (budget exhausted, dead-loop) are the exception and float out."""
        ...


@runtime_checkable
class GraphUnit(Unit, Protocol):
    """A Unit that can occupy a graph node — the interface the scheduler
    and the wire read: a display ``target``, a ``kind`` tag (``NodeKind``
    for the built-ins, a plain string for composed units), and the
    ``readonly`` wave-discipline flag (True: concurrent in a wave; False:
    serial; None: defer to the registry). Every built-in and every
    combinator satisfies this; a bare ``run``-only unit works anywhere a
    *caller* drives it, but a graph node asks for the whole face."""

    @property
    def kind(self) -> Any: ...

    @property
    def readonly(self) -> bool | None: ...

    @property
    def target(self) -> str: ...


# ════════════ the outcome ════════════


@dataclass(frozen=True, slots=True)
class Return:
    """Call-return: control comes back to the caller (spawn's semantics).

    The bare marker — there is nothing to configure about giving control
    back."""


@dataclass(frozen=True, slots=True)
class Handoff:
    """Control transfers to ``target`` and does not come back (peer's
    semantics): the sender's execution ends here, the target carries the
    chain.

    ``target`` is a live Unit reference inside one process. Crossing a run
    or process boundary lowers this to the serializable activation
    descriptor (target becomes a registry *name*) — the handoff vocabulary
    is kernel-level, the wire form is port-level, and the two never mix.
    ``carry`` says how much state travels: the full run state, or a
    filtered projection."""

    target: Unit
    carry: Literal["full", "filtered"] = "full"


Control = Return | Handoff
"""The explicit either/or a run's finish commits to. No default hiding."""


HANDOFF_ESCAPED = "__handoff_escaped__"
"""Sentinel ``Outcome.value`` for a caller abandoned mid-call by a handoff.

When a handoff bubbles up through an as_tool boundary, the abandoned
caller's own run cannot continue far enough to produce a meaningful value;
its Outcome carries this sentinel so drivers can tell "finished with this"
apart from "control left through me" (ruling 4 of REFACTOR-PLAN.md)."""


@dataclass(slots=True)
class Outcome:
    """What one Unit.run produces.

    ``value`` is the result the caller asked for. ``state_delta`` merges
    into the run's shared state under the declared reducer rules (a key
    written twice must say how — same discipline as ``Update``).
    ``control`` is the explicit return/handoff choice; ``Return`` is the
    default because delegation is the common case and taking over must be
    announced."""

    value: Any = None
    state_delta: dict[str, Any] = field(default_factory=dict)
    control: Control = field(default_factory=Return)

    def escaped(self) -> bool:
        """Did a handoff unwind through this outcome's producer?"""
        return bool(self.value == HANDOFF_ESCAPED)


# ════════════ raw→Outcome coercion ════════════


def coerce_result(raw: Any, *, tool: ToolName = "") -> ToolResult[Any]:
    """Coerce whatever a tool function returned into a ToolResult.

    A plain value is OK; a ToolResult/ToolError passes through; a dict can
    carry the control-flow markers (suspended / handoff / blocked / error).
    Everything else wraps as OK — this is the single throat tool output
    passes through before entering a run's transcript."""
    if isinstance(raw, ToolResult):
        return raw
    if isinstance(raw, ToolError):
        return ToolResult.from_error(raw, tool=tool)
    if isinstance(raw, dict):
        # Control-flow markers: a plain function can still steer the loop by
        # returning these dict shapes — no framework types required of it.
        if raw.get("suspended"):
            return ToolResult.suspended(
                reason=raw.get("reason", ""),
                tool=raw.get("tool", tool),
                approval_request_id=raw.get("approval_request_id", ""),
            )
        if raw.get("handoff"):
            return ToolResult.for_handoff(
                peer=raw.get("peer", ""),
                task=raw.get("task", ""),
                input_refs=raw.get("input_refs"),
                tool=raw.get("tool", tool),
            )
        if raw.get("blocked"):
            return ToolResult.blocked_by(raw.get("reason", ""), tool=raw.get("tool", tool))
        if raw.get("error"):
            raw_reason = raw.get("reason", "")
            err_val = raw.get("error")
            message = raw.get("message", "")
            if isinstance(err_val, str) and not message:
                message = err_val  # tolerate {"error": "text"} as the message form
            try:
                reason = ErrorReason(raw_reason)
            except ValueError:
                # Unknown reason strings degrade to UNKNOWN (still retryable)
                # rather than crashing the tool boundary.
                reason = ErrorReason.UNKNOWN
                message = message or f"invalid ErrorReason: {raw_reason!r}"
            return ToolResult.from_error(
                ToolError(
                    reason=reason,
                    code=raw.get("code", ""),
                    error_severity=ErrorSeverity.coerce(
                        raw.get("error_severity"),
                        default=(
                            ErrorSeverity.RED
                            if reason in NON_RETRYABLE_REASONS
                            else ErrorSeverity.YELLOW
                        ),
                    ),
                    message=message,
                    hint=raw.get("hint", ""),
                ),
                tool=tool,
            )
    return ToolResult(ToolOutcome.OK, value=raw, tool=tool)


# ════════════ the streaming form ════════════


@runtime_checkable
class StreamingUnit(Protocol):
    """Optional companion form of :meth:`Unit.run` for units with live events.

    ``run_stream`` is the *same* execution as ``run``, exposed as an async
    generator: live events (a ReAct loop's Turns) are yielded as they
    happen and the :class:`Outcome` is appended to ``box`` exactly once at
    the end (a call-by-reference return — generator control flow cannot
    also return a value). Drivers that stream prefer this form and run it
    *inline on their own stack*: suspending the driver suspends the unit
    with it — abandoning a stream must freeze the work, never orphan it
    (a detached task would keep firing side effects and writing
    checkpoints nobody is reading)."""

    def run_stream(
        self, input: Any, ctx: UnitContext, box: list[Outcome]
    ) -> AsyncGenerator[AgentEvent, None]: ...


# ════════════ the context ════════════


@dataclass(slots=True)
class UnitContext:
    """The execution wiring one Unit may draw on — never run data.

    What a context holds: who is executing (``run_id``/``node_id``), the
    live run it reads from (conversation, metrics — the react loop writes
    turns there), a read view of shared state, and the collaborator slots
    the composition root fills (the tool throat, the model invoker, the
    activation slot, the Turn engine, the fn table). ``emit`` is where
    ``fire`` sends live events — a driver that streams attaches its sink
    here, and one that doesn't leaves it None (events drop).

    What it never holds: anything a checkpoint would need. The context is
    reconstructed from configuration on every resume; only the run and its
    plan persist. That is the whole reason collaborators ride the context
    instead of being baked into every Unit declaration."""

    run_id: str
    node_id: str = ""
    run: Run | None = None
    shared: Mapping[str, Any] = field(default_factory=dict)
    """Read view of the run's shared state (write goes through state_delta)."""
    tools: ToolExecutor | None = None
    llm: LLMInvoker | None = None
    subagent: SubagentInvoker | None = None
    engine: AutonomyEngine | None = None
    """The agent-loop engine slot (what AutonomousUnit drives)."""
    fns: Mapping[str, Callable[..., Any]] = field(default_factory=dict)
    emit: Callable[[AgentEvent], None] | None = None

    def fire(self, event: AgentEvent) -> None:
        """Send a live event to the sink if one is attached."""
        if self.emit is not None:
            self.emit(event)


# ════════════ the registry metadata ════════════


@dataclass(frozen=True, slots=True)
class UnitMeta:
    """Registry metadata for a Unit — how it is named and what it costs.

    ``is_agentic`` is the heavy/light flag the scheduler uses for budget
    and parallelism decisions: True means this unit may burn model budget
    (an agent, an autonomous loop). Unknown units default to True — treat
    unlabelled as expensive; the cheap case announces itself.

    ``readonly`` is the wave-discipline flag: True runs concurrently in a
    wave, False serializes with other writers, None defers to the tool
    registry's metadata (the tool-body case) and defaults to serial."""

    name: str
    description: str = ""
    is_agentic: bool = True
    readonly: bool | None = None


__all__ = [
    "Unit",
    "GraphUnit",
    "AutonomyEngine",
    "Outcome",
    "Return",
    "Handoff",
    "Control",
    "HANDOFF_ESCAPED",
    "UnitContext",
    "StreamingUnit",
    "UnitMeta",
    "ToolExecutor",
    "LLMInvoker",
    "SubagentInvoker",
]
