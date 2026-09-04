"""Run — the single run-state object."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, ClassVar, Generic

from typing_extensions import TypeVar

from prodagent.base.codec import dump, load
from prodagent.base.determinism import now_monotonic, now_wall
from prodagent.base.errors import ClassifiedError, ErrorLayer, IllegalTransition, classify_error
from prodagent.kernel.interrupt import Interrupt
from prodagent.kernel.node_state import NodeRuntimeState
from prodagent.kernel.types import (
    LLMResponse,
    MessageList,
    RunCompletedEvent,
    RunFailedEvent,
    RunState,
    RunSuspendedEvent,
    ToolCall,
)

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator

    from prodagent.base.types import JsonDict
    from prodagent.kernel.types import AgentEvent

logger = logging.getLogger(__name__)

CHILD_SEPARATOR = "::"

_TERMINAL_ERROR = "run ended without a terminal event"

MARKER_TAIL_CURSOR = "terminal"
"""The boxed cursor key holding the loop recipe's marker tail (round and
terminal markers). Plan events interleave with those markers on the SAME
stream (``<run_id>``), and the plan cursor's ``last_seq`` tracks that
stream too — so the one tail is kept in both boxes, advanced in lockstep
through :meth:`Run.advance_marker_tail` and read as one number through
:meth:`Run.marker_tail`."""

RUN_SCHEMA_VERSION = 3
"""Serialization format of ``Run.to_dict``. Bumped when the dict shape
changes in a way old loaders would misread. A checkpoint written by a newer
schema loads best-effort (fields it doesn't know are ignored); readers warn
on a higher version rather than refusing — a checkpoint that loads wrong is
recoverable, one that refuses to load is not."""


def is_child_run_id(run_id: str) -> bool:
    """True when run_id is a child-agent-scoped id (parent::child)."""
    return CHILD_SEPARATOR in run_id


def make_failed_run(run_id: str, task: str, *, last_error: str = _TERMINAL_ERROR) -> Run:
    """Synthetic FAILED run for a stream that ended without a terminal event."""
    return Run(run_id=run_id, task=task, state=RunState.FAILED, last_error=last_error)


async def collect_final_run(
    stream: AsyncGenerator[AgentEvent, None],
    *,
    fallback_run_id: str,
    fallback_task: str,
) -> Run:
    """Reduce an event stream to its terminal run — the last COMPLETED/FAILED/
    SUSPENDED event wins; a stream that ends without one yields a synthetic
    FAILED run. Shared by every "drive to terminal state" entry point."""
    final_run: Run | None = None
    async for event in stream:
        if isinstance(event, (RunCompletedEvent, RunFailedEvent, RunSuspendedEvent)):
            final_run = event.run
    if final_run is None:
        return make_failed_run(fallback_run_id, fallback_task)
    return final_run


def child_run_id(parent_run_id: str, child_name: str) -> str:
    """Child ids embed their parent's (``parent::child``) — attribution for
    accounting and governance readable from the id alone."""
    return f"{parent_run_id}{CHILD_SEPARATOR}{child_name}"


def is_child_subordinate(run: Run) -> bool:
    """Spawned child run: its side-effects and flushes are owned by the parent."""
    return run.parent_run_id is not None


# ── Parks — where a run is stopped awaiting the world ────────────────────────
#
# A run parks in exactly one situation: an Interrupt (HITL approval, a
# question, an external wait) — ONE durable fact on this object, not a
# family of fields. Control transfer is NOT a park — a handoff is a command
# the scheduler applies to the plan, so the plan cursor is the chain's
# resume point, never a field here.


def _cursors_from_dict(d: JsonDict) -> dict[str, Any]:
    """The boxed ``cursors`` section, read straight off the wire."""
    return dict(d.get("cursors") or {})


def _toolcall_to_dict(call: ToolCall | JsonDict) -> JsonDict:
    if isinstance(call, dict):
        return call
    return call.to_dict()


def _toolcall_from_dict(d: ToolCall | JsonDict) -> ToolCall:
    if isinstance(d, ToolCall):
        return d
    return ToolCall.from_dict(d)


def _interrupt_from_wire(d: JsonDict) -> Interrupt | None:
    """Read the park fact off a checkpoint — no legacy mapping: a checkpoint
    without the ``interrupt`` wire simply wasn't parked."""
    return Interrupt.from_dict(d.get("interrupt"))


@dataclass
class RunMetrics:
    """Token/cost/turn accounting for a run — a single cohesive unit consumed
    by budget checks and spawn accounting."""

    turn_count: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    cost_usd: float = 0.0

    @property
    def cache_hit_ratio(self) -> float:
        """Fraction of input tokens served from cache (0 when there's no input yet)."""
        return self.cache_read_tokens / max(1, self.input_tokens)

    def to_dict(self) -> JsonDict:
        return dump(self)

    @classmethod
    def from_dict(cls, d: JsonDict | None) -> RunMetrics:
        if d is None:
            return cls()
        return load(cls, d)


_RunT = TypeVar("_RunT", default=Any)


@dataclass
class SchedulerCursor:
    """One executor's resumption tail — the typed view of a boxed cursor.

    The plan executor's section is the load-bearing one: the plan snapshot
    (the wire form ``Plan.to_state`` produces) plus the event-log seq it was
    taken at, so restore folds the snapshot first and replays only the tail
    (snapshot-plus-tail, the WAL trade). The wire shape stays
    ``{"state": ..., "last_seq": ...}`` — checkpoints pre-dating this class
    load unchanged."""

    state: dict[str, Any] | None = None
    last_seq: int = 0

    def to_wire(self) -> dict[str, Any]:
        return {"state": self.state, "last_seq": self.last_seq}

    @classmethod
    def from_wire(cls, d: Any) -> SchedulerCursor:
        if not isinstance(d, dict):
            return cls()
        return cls(state=d.get("state"), last_seq=int(d.get("last_seq") or 0))


@dataclass
class Run(Generic[_RunT]):
    """Central mutable state object for one execution of one unit.

    Not agent-specific: any NodeBody a scheduler drives (the five built-ins, an
    Agent, a subgraph) runs as a Run. ``unit_ref`` names what executes —
    the registry name (an agent's name, a workflow id) where one exists,
    "" for the legacy task-only shape; it rides the checkpoint so a resumed
    run still knows what it was executing."""

    run_id: str
    task: str
    state: RunState = RunState.RUNNING
    unit_ref: str = ""
    """Registry name of the unit this run executes ("" when unnamed) — a
    reference by NAME, never a live object: the run is checkpointable, the
    unit is reconstructed from configuration at resume (ruling 3)."""

    metrics: RunMetrics = field(default_factory=RunMetrics)
    start_time: float = field(default_factory=now_wall)
    """Wall-clock start — persisted for display and as the resume baseline."""
    monotonic_start: float | None = field(default_factory=now_monotonic, repr=False, compare=False)
    """NTP-immune in-process anchor for :meth:`elapsed_seconds`. ``None`` on a
    run deserialized from a checkpoint (monotonic clocks are process-relative),
    in which case elapsed falls back to wall-clock against ``start_time`` —
    a resumed run's deadline still binds, counting the downtime. Rebase
    ``start_time`` at the resume site if downtime should be forgiven."""
    parent_run_id: str | None = None
    depth: int = 0
    """Run-tree depth: 0 at a root, +1 per delegation hop. persisted so a
    resumed tree still knows its shape (attribution and spawn budgets
    read it)."""

    # Mutable working transcript during this turn; copied whole into
    # ConversationSession.messages at complete_turn. See session.py docstring.
    messages: MessageList = field(default_factory=list)
    tool_history: list[ToolCall] = field(default_factory=list)
    tool_failures: int = 0
    last_action: str | None = None
    retry_counter: dict[str, int] = field(default_factory=dict)
    fingerprints: list[str] = field(default_factory=list)
    idempotency_seq: int = 0
    interrupt: Interrupt | None = None
    """The ONE durable park fact — what a suspended run is waiting on
    (:class:`~prodagent.kernel.interrupt.Interrupt`: kind, request id, the
    node it parked at, and the staged action in its payload). Not smeared
    across fields: the frozen call is the payload of an approve, the
    request id rides the fact, the parked node is a structural field.
    Checkpoints written before this field existed upgrade on read from the
    historical three-field wire."""
    last_error: str | None = None
    error: ClassifiedError | None = None
    cursors: dict[str, Any] = field(default_factory=dict)
    """Per-executor resumption tails, boxed: each execution mode owns ONE key
    here and the shape of its value (JSON-able; checkpointed as its own
    section so each mode's cursor evolves without touching this object or
    bumping anyone else's schema). Keys in use: ``plan`` (PlanEventLog —
    ``{"state": JsonDict | None, "last_seq": int}``) and ``terminal``
    (the loop recipe's marker tail — an int); the two track ONE stream and
    move together through :meth:`marker_tail` / :meth:`advance_marker_tail`."""
    node_states: dict[str, NodeRuntimeState] = field(default_factory=dict)
    """Per-node execution state — the mutable half of every Node in the plan
    this run executes. The blueprint stays static and shareable; progress
    lives here, on the run. Resumed runs materialize this from the plan
    cursor's state at bootstrap."""
    shared: dict[str, Any] = field(default_factory=dict)
    """The run's shared state — what Update commands merge into (column 9's
    third dynamic action). Typed values keyed by name; conflicts resolve
    through declared reducers at the gate, never by silent overwrite."""
    checkpoint_version: int = 0
    checkpoint_failed: bool = False

    final_output: str | None = None
    structured_output: _RunT | None = None

    @property
    def turn_count(self) -> int:
        return self.metrics.turn_count

    @property
    def input_tokens(self) -> int:
        return self.metrics.input_tokens

    @property
    def output_tokens(self) -> int:
        return self.metrics.output_tokens

    @property
    def cache_read_tokens(self) -> int:
        return self.metrics.cache_read_tokens

    @property
    def cache_write_tokens(self) -> int:
        return self.metrics.cache_write_tokens

    @property
    def cost_usd(self) -> float:
        return self.metrics.cost_usd

    def retry_count(self, tool_name: str) -> int:
        return self.retry_counter.get(tool_name, 0)

    # ── Node progress — the run owns how far each node got ────────────────

    def node_state(self, node_id: str) -> NodeRuntimeState:
        """One node's execution state, vacuously PENDING when untouched so
        far — callers never branch on "has this node been seen"."""
        st = self.node_states.get(node_id)
        if st is None:
            st = NodeRuntimeState(node_id)
            self.node_states[node_id] = st
        return st

    # ── Terminal transitions — the single throat ────────────────────────────
    # State flips used to live in ~16 scattered assignments; the pairings
    # (FAILED ↔ last_error, COMPLETED ↔ final_output, RUNNING ↔ no stale
    # crash scene) held only by convention at every site. They hold here now,
    # behind the explicit allowed-transition table (column 8): a door is
    # legality + pairing + flip in one move, and anything outside the table
    # is an illegal transition, loudly, at the write site.

    _ALLOWED: ClassVar[dict[RunState, frozenset[RunState]]] = {
        RunState.RUNNING: frozenset(
            {RunState.RUNNING, RunState.COMPLETED, RunState.FAILED, RunState.SUSPENDED}
        ),
        # RUNNING→RUNNING: crash recovery resumes a mid-flight run — the
        # unknown partial state is redone, and the door clears the crash
        # scene as part of the pairing.
        RunState.SUSPENDED: frozenset({RunState.RUNNING, RunState.COMPLETED}),
        # SUSPENDED→RUNNING is the interrupt's way back; SUSPENDED→COMPLETED
        # is the handoff door — a transfer outranks a parked decision and
        # finishes the run it was suspending.
        RunState.COMPLETED: frozenset({RunState.RUNNING, RunState.FAILED}),
        # COMPLETED→RUNNING: a session's next turn re-drives its run (this
        # framework reuses the run id across a conversation — the strict
        # "terminal is history" reading arrives with per-turn run ids).
        # COMPLETED→FAILED: the late governance veto — an output contract
        # checked at settle fails a "done" run rather than let an
        # unacceptable artifact stand as a success.
        RunState.FAILED: frozenset({RunState.RUNNING}),
        # FAILED→RUNNING: crash recovery's redo (column 19's at-least-once) —
        # a crashed attempt's checkpoint resumes and redoes the unknown tail.
        # What stays illegal everywhere: ending *from* suspended into failed,
        # and failed into completed — a dead end never turns into success.
    }

    def _transition(self, target: RunState) -> None:
        if target is self.state:
            return  # idempotent re-settle: not a transition, a no-op
        if target not in Run._ALLOWED[self.state]:
            raise IllegalTransition(
                f"run {self.run_id!r}: {self.state.value} → {target.value} is not a "
                "legal transition (terminal states only leave as history; a "
                "suspended run resumes to RUNNING before it can end)"
            )
        self.state = target

    def complete(self, final_output: str | None = None, *, backfill: bool = False) -> None:
        """Transition to COMPLETED. A falsy *final_output* keeps whatever is
        already set; ``backfill=True`` takes the last non-empty assistant
        content instead — a run cut off by max_tokens still deserves an
        answer to show."""
        self._transition(RunState.COMPLETED)
        if final_output:
            self.final_output = final_output
        if not self.final_output and backfill:
            for msg in reversed(self.messages):
                if msg.get("role") == "assistant" and msg.get("content"):
                    content = msg["content"]
                    self.final_output = content if isinstance(content, str) else str(content)
                    break

    def fail(self, reason: str | BaseException) -> None:
        """Transition to FAILED — the run's crash scene (last_error, and a
        classified error for exceptions) is part of the pairing, not an
        optional extra the caller might forget."""
        self._transition(RunState.FAILED)
        self.last_error = str(reason)
        if isinstance(reason, BaseException):
            self.error = classify_error(reason, layer=ErrorLayer.RUNTIME)

    def suspend(self, reason: str = "") -> None:
        """Transition to SUSPENDED — awaiting the world (a HITL decision).
        Softer pairing than the others: the plan-approval path suspends
        before its interrupt fact lands, so the invariant is "awaiting",
        not "has an interrupt"."""
        self._transition(RunState.SUSPENDED)
        if reason:
            self.last_error = reason

    def resume(self) -> None:
        """Back to RUNNING (checkpoint resume) — a resumed run must not
        inherit the previous attempt's crash scene."""
        self._transition(RunState.RUNNING)
        self.last_error = None
        self.error = None

    # ── Resume-point parking — the invariant's single home ──────────────────

    @property
    def pending_approval_id(self) -> str | None:
        """The outstanding HITL request id — a derived READ of the park
        fact, never stored separately (the approval-correlation surfaces —
        gates, summaries, the playground — all mean "the interrupt's
        request")."""
        return self.interrupt.request_id if self.interrupt is not None else None

    def park(self, interrupt: Interrupt) -> bool:
        """Park awaiting the world on ``interrupt`` (column 20's letting-go).

        Refuses — ``False``, nothing changes — when the run is already
        parked (a second suspension never moves the first park; a node
        refines the parked fact via ``dataclasses.replace`` instead).
        Callers keep their own bookkeeping (history pruning, plan events)
        outside this method."""
        if self.state is RunState.SUSPENDED:
            return False
        self.suspend()
        self.interrupt = interrupt
        return True

    def take_interrupt(self) -> Interrupt | None:
        """Consume the park — the resume path re-drives from this fact (the
        approval's staged call retries verbatim, never a re-ask of the
        model)."""
        iv = self.interrupt
        self.interrupt = None
        return iv

    def increment_retry(self, tool_name: str) -> int:
        c = self.retry_counter.get(tool_name, 0) + 1
        self.retry_counter[tool_name] = c
        return c

    def reset_retry(self, tool_name: str) -> None:
        self.retry_counter[tool_name] = 0

    def cursor(self, key: str, default: Any = None) -> Any:
        """Read one executor's boxed cursor section (``None``/default when absent)."""
        return self.cursors.get(key, default)

    def set_cursor(self, key: str, value: Any) -> None:
        """Write one executor's boxed cursor section."""
        self.cursors[key] = value

    def plan_cursor(self) -> SchedulerCursor:
        """The plan executor's resumption tail, typed."""
        return SchedulerCursor.from_wire(self.cursor("plan"))

    def set_plan_cursor(self, cursor: SchedulerCursor) -> None:
        """Write the plan executor's resumption tail (same wire shape as
        always — v1 checkpoints and this class agree on the dict)."""
        self.set_cursor("plan", cursor.to_wire())

    def marker_tail(self) -> int:
        """The marker stream's ONE tail seq (0 = empty).

        Plan events and the loop recipe's round/terminal markers interleave
        on the run's stream, so the plan cursor and the loop's own box must
        never drift apart — a graph shape whose work node is a loop had the
        plan tail at 4 while the loop's box sat at 0, and the first round
        marker died on VersionConflict. ``max`` reads through a box that a
        legacy checkpoint left half-advanced; every writer since keeps the
        two in lockstep via :meth:`advance_marker_tail`."""
        return max(self.plan_cursor().last_seq, int(self.cursor(MARKER_TAIL_CURSOR, 0) or 0))

    def advance_marker_tail(self, seq: int) -> None:
        """Record the tail an append returned — both boxes, one number."""
        self.set_plan_cursor(SchedulerCursor(state=self.plan_cursor().state, last_seq=seq))
        self.set_cursor(MARKER_TAIL_CURSOR, seq)

    def push_fingerprint(self, fp: str, *, window: int) -> int:
        """Append a tool-call fingerprint to the sliding window and return how
        many times it now occurs within that window (the dead-loop count)."""
        self.fingerprints.append(fp)
        if len(self.fingerprints) > window:
            # Slide: keep only the newest `window` fingerprints — ancient
            # repeats must not count against a current loop.
            del self.fingerprints[: len(self.fingerprints) - window]
        return self.fingerprints.count(fp)

    @property
    def total_tokens(self) -> int:
        return self.metrics.input_tokens + self.metrics.output_tokens

    def elapsed_seconds(self) -> float:
        """Two clocks, one rule: monotonic while the process lives (NTP
        can't bend it), wall-clock after a checkpoint restore (monotonic is
        process-relative) — so a resumed run's seconds axis still binds."""
        if self.monotonic_start is not None:
            return now_monotonic() - self.monotonic_start
        return now_wall() - self.start_time

    def add_tokens(self, response: LLMResponse, *, cost_usd: float) -> None:
        """cost_usd is pre-computed by the caller from the model's pricing,
        keeping core free of any LLM package type."""
        self.metrics.input_tokens += response.input_tokens
        self.metrics.output_tokens += response.output_tokens
        self.metrics.cache_read_tokens += response.cache_read_tokens
        self.metrics.cache_write_tokens += response.cache_write_tokens
        self.metrics.cost_usd += cost_usd

    def to_dict(self) -> JsonDict:
        """Durable subset needed to resume a crashed run losslessly:
        transcript, retry/fingerprint/pending counters (no double side
        effects / lost approval / lost loop memory), and error/last_error
        (crash scene)."""
        return {
            "schema_version": RUN_SCHEMA_VERSION,
            "run_id": self.run_id,
            "task": self.task,
            "state": self.state.value,
            "unit_ref": self.unit_ref,
            "messages": list(self.messages),
            "tool_history": [_toolcall_to_dict(c) for c in self.tool_history],
            "final_output": self.final_output,
            "structured_output": (
                self.structured_output.model_dump()
                if self.structured_output is not None
                and hasattr(self.structured_output, "model_dump")
                else None
            ),
            "metrics": self.metrics.to_dict(),
            "parent_run_id": self.parent_run_id,
            "depth": self.depth,
            "tool_failures": self.tool_failures,
            "last_action": self.last_action,
            "start_time": self.start_time,
            "retry_counter": dict(self.retry_counter),
            "fingerprints": list(self.fingerprints),
            "idempotency_seq": self.idempotency_seq,
            "interrupt": self.interrupt.to_dict() if self.interrupt is not None else None,
            "last_error": self.last_error,
            "error": self.error.to_dict() if self.error is not None else None,
            "cursors": dict(self.cursors),
            "shared": dict(self.shared),
        }

    @classmethod
    def from_dict(cls, d: JsonDict) -> Run[Any]:
        stored_schema = d.get("schema_version", RUN_SCHEMA_VERSION)
        if stored_schema > RUN_SCHEMA_VERSION:
            logger.warning(
                "Run.from_dict: checkpoint schema v%s is newer than this "
                "loader (v%s) — loading best-effort; unknown fields ignored",
                stored_schema,
                RUN_SCHEMA_VERSION,
            )
        return cls(
            run_id=d["run_id"],
            task=d["task"],
            state=RunState(d.get("state", RunState.RUNNING.value)),
            unit_ref=d.get("unit_ref", ""),
            messages=list(d.get("messages", [])),
            tool_history=[_toolcall_from_dict(c) for c in d.get("tool_history", [])],
            final_output=d.get("final_output"),
            structured_output=d.get("structured_output"),
            metrics=RunMetrics.from_dict(d.get("metrics")),
            parent_run_id=d.get("parent_run_id"),
            depth=int(d.get("depth", 0) or 0),
            tool_failures=d.get("tool_failures", 0),
            last_action=d.get("last_action"),
            start_time=d.get("start_time", now_wall()),
            monotonic_start=None,  # process-relative clock meaningless across a restore
            retry_counter=dict(d.get("retry_counter", {})),
            fingerprints=list(d.get("fingerprints", [])),
            idempotency_seq=d.get("idempotency_seq", 0),
            interrupt=_interrupt_from_wire(d),
            last_error=d.get("last_error"),
            error=(
                ClassifiedError.from_dict(d["error"]) if isinstance(d.get("error"), dict) else None
            ),
            cursors=_cursors_from_dict(d),
            shared=dict(d.get("shared") or {}),
        )
