"""AgentRun — the single run-state object."""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Generic

from typing_extensions import TypeVar

from prodagent.base.codec import dump, load
from prodagent.base.errors import ClassifiedError, ErrorLayer, classify_error
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

AGENT_RUN_SCHEMA_VERSION = 2
"""Serialization format of ``AgentRun.to_dict``. Bumped when the dict shape
changes in a way old loaders would misread. A checkpoint written by a newer
schema loads best-effort (fields it doesn't know are ignored); readers warn
on a higher version rather than refusing — a checkpoint that loads wrong is
recoverable, one that refuses to load is not.

v2 boxes the per-executor resumption tails into one ``cursors`` section
(v1 carried them flat: ``plan_state`` / ``plan_last_seq`` /
``last_event_seq``); v1 checkpoints migrate on load."""


def is_child_run_id(run_id: str) -> bool:
    """True when run_id is a child-agent-scoped id (parent::child)."""
    return CHILD_SEPARATOR in run_id


def make_failed_run(run_id: str, task: str, *, last_error: str = _TERMINAL_ERROR) -> AgentRun:
    """Synthetic FAILED run for a stream that ended without a terminal event."""
    return AgentRun(run_id=run_id, task=task, state=RunState.FAILED, last_error=last_error)


async def collect_final_run(
    stream: AsyncGenerator[AgentEvent, None],
    *,
    fallback_run_id: str,
    fallback_task: str,
) -> AgentRun:
    """Reduce an event stream to its terminal run — the last COMPLETED/FAILED/
    SUSPENDED event wins; a stream that ends without one yields a synthetic
    FAILED run. Shared by every "drive to terminal state" entry point."""
    final_run: AgentRun | None = None
    async for event in stream:
        if isinstance(event, (RunCompletedEvent, RunFailedEvent, RunSuspendedEvent)):
            final_run = event.run
    if final_run is None:
        return make_failed_run(fallback_run_id, fallback_task)
    return final_run


def child_run_id(parent_run_id: str, child_name: str) -> str:
    return f"{parent_run_id}{CHILD_SEPARATOR}{child_name}"


def is_child_subordinate(run: AgentRun) -> bool:
    """Child-agent run whose side-effects are owned by the parent (not a peer continuation)."""
    return run.parent_run_id is not None and not run.is_peer_continuation


@dataclass
class PendingHandoff:
    """A run's pending transfer of control to a peer agent."""

    peer_name: str
    task: str
    input_refs: dict[str, str] = field(default_factory=dict)
    prior_output: str = ""
    peer_run_id: str | None = None
    message_id: str = ""
    """Identity of the relay crossing — minted once when the handoff tool
    fires, reused by the relay (and by crash-replay suppression). Checkpoints
    written before this field existed load with "" and mint at relay time."""

    def to_dict(self) -> JsonDict:
        return dump(self)

    @classmethod
    def from_dict(cls, d: JsonDict | None) -> PendingHandoff | None:
        if d is None:
            return None
        if isinstance(d, PendingHandoff):
            return d
        return load(cls, d)


# ── Resume points — where a run is parked awaiting the world ─────────────────
#
# A run parks in exactly one of two situations: awaiting HITL approval
# (retry this exact call once approved) or awaiting a peer relay (this run is
# finished, control transfers). The storage is three nullable fields (kept
# for checkpoint compatibility); the invariant — at most one logically
# active park, handoff outranking approval — is enforced by the park methods
# below and read through the typed :meth:`AgentRun.resume_point` view.


@dataclass(frozen=True, slots=True)
class AwaitingApproval:
    """Paused mid-batch: retry this exact call once the decision arrives."""

    call: ToolCall
    request_id: str | None


@dataclass(frozen=True, slots=True)
class AwaitingHandoff:
    """Control transfers to a peer; this run is finished."""

    handoff: PendingHandoff


ResumePoint = AwaitingApproval | AwaitingHandoff | None


def _cursors_from_dict(d: JsonDict) -> dict[str, Any]:
    """v2 reads the boxed ``cursors`` section; a v1 dict (flat ``plan_state`` /
    ``plan_last_seq`` / ``last_event_seq``) migrates into it on load."""
    if "cursors" in d:
        return dict(d.get("cursors") or {})
    cursors: dict[str, Any] = {}
    plan_state = d.get("plan_state")
    plan_last_seq = d.get("plan_last_seq", 0)
    if plan_state is not None or plan_last_seq:
        cursors["plan"] = {"state": plan_state, "last_seq": plan_last_seq}
    if d.get("last_event_seq", 0):
        cursors["reactive"] = d.get("last_event_seq", 0)
    return cursors


def _toolcall_to_dict(call: ToolCall | JsonDict) -> JsonDict:
    if isinstance(call, dict):
        return call
    return call.to_dict()


def _toolcall_from_dict(d: ToolCall | JsonDict) -> ToolCall:
    if isinstance(d, ToolCall):
        return d
    return ToolCall.from_dict(d)


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
class AgentRun(Generic[_RunT]):
    """Central mutable state object for a single agent execution."""

    run_id: str
    task: str
    state: RunState = RunState.RUNNING

    metrics: RunMetrics = field(default_factory=RunMetrics)
    start_time: float = field(default_factory=time.time)
    """Wall-clock start — persisted for display and as the resume baseline."""
    monotonic_start: float | None = field(default_factory=time.monotonic, repr=False, compare=False)
    """NTP-immune in-process anchor for :meth:`elapsed_seconds`. ``None`` on a
    run deserialized from a checkpoint (monotonic clocks are process-relative),
    in which case elapsed falls back to wall-clock against ``start_time`` —
    a resumed run's deadline still binds, counting the downtime. Rebase
    ``start_time`` at the resume site if downtime should be forgiven."""
    parent_run_id: str | None = None

    # Mutable working transcript during this turn; copied whole into
    # ConversationSession.messages at complete_turn. See session.py docstring.
    messages: MessageList = field(default_factory=list)
    tool_history: list[ToolCall] = field(default_factory=list)
    tool_failures: int = 0
    last_action: str | None = None
    retry_counter: dict[str, int] = field(default_factory=dict)
    fingerprints: list[str] = field(default_factory=list)
    idempotency_seq: int = 0
    pending_tool_call: ToolCall | None = None
    pending_approval_id: str | None = None
    pending_handoff: PendingHandoff | None = None
    last_error: str | None = None
    error: ClassifiedError | None = None
    cursors: dict[str, Any] = field(default_factory=dict)
    """Per-executor resumption tails, boxed: each execution mode owns ONE key
    here and the shape of its value (JSON-able; checkpointed as its own
    section so each mode's cursor evolves without touching this object or
    bumping anyone else's schema). Keys in use: ``plan`` (PlanEventLog —
    ``{"state": JsonDict | None, "last_seq": int}``), ``reactive``
    (ReactiveLoop's event-log tail seq — an int)."""
    checkpoint_version: int = 0
    checkpoint_failed: bool = False

    final_output: str | None = None
    structured_output: _RunT | None = None
    is_peer_continuation: bool = False

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

    # ── Terminal transitions — the single throat ────────────────────────────
    # State flips used to live in ~16 scattered assignments; the pairings
    # (FAILED ↔ last_error, COMPLETED ↔ final_output, RUNNING ↔ no stale
    # crash scene) held only by convention at every site. They hold here now.

    def complete(self, final_output: str | None = None, *, backfill: bool = False) -> None:
        """Transition to COMPLETED. A falsy *final_output* keeps whatever is
        already set; ``backfill=True`` takes the last non-empty assistant
        content instead — a run cut off by max_tokens still deserves an
        answer to show."""
        self.state = RunState.COMPLETED
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
        self.state = RunState.FAILED
        self.last_error = str(reason)
        if isinstance(reason, BaseException):
            self.error = classify_error(reason, layer=ErrorLayer.RUNTIME)

    def suspend(self, reason: str = "") -> None:
        """Transition to SUSPENDED — awaiting the world (HITL decision or a
        relay). Softer pairing than the others: the plan-approval path
        suspends without a parked call, so the invariant is "awaiting",
        not "has a resume_point"."""
        self.state = RunState.SUSPENDED
        if reason:
            self.last_error = reason

    def revive(self) -> None:
        """Back to RUNNING (checkpoint resume) — a revived run must not
        inherit the previous attempt's crash scene."""
        self.state = RunState.RUNNING
        self.last_error = None
        self.error = None

    # ── Resume-point parking — the invariant's single home ──────────────────

    def resume_point(self) -> ResumePoint:
        """Typed view of where this run is parked, if anywhere.

        A handoff outranks an approval: if a racing batch parked both (only
        the plan executor can), the chain continues at the peer and the
        parked call is abandoned.
        """
        if self.pending_handoff is not None:
            return AwaitingHandoff(self.pending_handoff)
        if self.pending_tool_call is not None:
            return AwaitingApproval(self.pending_tool_call, self.pending_approval_id)
        return None

    def park_for_approval(self, call: ToolCall, request_id: str | None) -> bool:
        """Park awaiting a HITL decision on ``call``. Refuses — ``False``,
        nothing changes — when the run is already parked (a pending handoff
        outranks an approval; a second suspension never moves the first
        parked call). Callers keep their own bookkeeping (history pruning,
        plan events) outside this method.
        """
        if self.pending_handoff is not None or self.state is RunState.SUSPENDED:
            return False
        self.suspend()
        self.pending_tool_call = call
        self.pending_approval_id = request_id
        return True

    def park_handoff(self, handoff: PendingHandoff) -> bool:
        """Park a peer transfer — first handoff wins. Overwrites an approval
        park (a transfer outranks a pending decision) and finishes the run."""
        if self.pending_handoff is not None:
            return False
        self.complete(f"Handed off to {handoff.peer_name}" if handoff.peer_name else "Handed off")
        self.pending_handoff = handoff
        return True

    def clear_approval_park(self) -> AwaitingApproval | None:
        """Consume the approval park — the resume path retries the returned
        call once the decision is in. Handoff parks are consumed by the relay."""
        if self.pending_tool_call is None:
            return None
        park = AwaitingApproval(self.pending_tool_call, self.pending_approval_id)
        self.pending_tool_call = None
        self.pending_approval_id = None
        return park

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

    def push_fingerprint(self, fp: str, *, window: int) -> int:
        """Append a tool-call fingerprint to the sliding window and return how
        many times it now occurs within that window (the dead-loop count)."""
        self.fingerprints.append(fp)
        if len(self.fingerprints) > window:
            del self.fingerprints[: len(self.fingerprints) - window]
        return self.fingerprints.count(fp)

    @property
    def total_tokens(self) -> int:
        return self.metrics.input_tokens + self.metrics.output_tokens

    def elapsed_seconds(self) -> float:
        if self.monotonic_start is not None:
            return time.monotonic() - self.monotonic_start
        return time.time() - self.start_time

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
            "schema_version": AGENT_RUN_SCHEMA_VERSION,
            "run_id": self.run_id,
            "task": self.task,
            "state": self.state.value,
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
            "tool_failures": self.tool_failures,
            "last_action": self.last_action,
            "start_time": self.start_time,
            "retry_counter": dict(self.retry_counter),
            "fingerprints": list(self.fingerprints),
            "idempotency_seq": self.idempotency_seq,
            "pending_tool_call": (
                self.pending_tool_call.to_dict() if self.pending_tool_call else None
            ),
            "pending_approval_id": self.pending_approval_id,
            "pending_handoff": self.pending_handoff.to_dict() if self.pending_handoff else None,
            "last_error": self.last_error,
            "error": self.error.to_dict() if self.error is not None else None,
            "cursors": dict(self.cursors),
            "is_peer_continuation": self.is_peer_continuation,
        }

    @classmethod
    def from_dict(cls, d: JsonDict) -> AgentRun[Any]:
        stored_schema = d.get("schema_version", AGENT_RUN_SCHEMA_VERSION)
        if stored_schema > AGENT_RUN_SCHEMA_VERSION:
            logger.warning(
                "AgentRun.from_dict: checkpoint schema v%s is newer than this "
                "loader (v%s) — loading best-effort; unknown fields ignored",
                stored_schema,
                AGENT_RUN_SCHEMA_VERSION,
            )
        return cls(
            run_id=d["run_id"],
            task=d["task"],
            state=RunState(d.get("state", RunState.RUNNING.value)),
            messages=list(d.get("messages", [])),
            tool_history=[_toolcall_from_dict(c) for c in d.get("tool_history", [])],
            final_output=d.get("final_output"),
            structured_output=d.get("structured_output"),
            metrics=RunMetrics.from_dict(d.get("metrics")),
            parent_run_id=d.get("parent_run_id"),
            tool_failures=d.get("tool_failures", 0),
            last_action=d.get("last_action"),
            start_time=d.get("start_time", time.time()),
            monotonic_start=None,  # process-relative clock meaningless across a restore
            retry_counter=dict(d.get("retry_counter", {})),
            fingerprints=list(d.get("fingerprints", [])),
            idempotency_seq=d.get("idempotency_seq", 0),
            pending_tool_call=(
                _toolcall_from_dict(d["pending_tool_call"]) if d.get("pending_tool_call") else None
            ),
            pending_approval_id=d.get("pending_approval_id"),
            pending_handoff=PendingHandoff.from_dict(d.get("pending_handoff")),
            last_error=d.get("last_error"),
            error=(
                ClassifiedError.from_dict(d["error"]) if isinstance(d.get("error"), dict) else None
            ),
            cursors=_cursors_from_dict(d),
            is_peer_continuation=d.get("is_peer_continuation", False),
        )
