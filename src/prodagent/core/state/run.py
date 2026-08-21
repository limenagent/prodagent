"""AgentRun — the single run-state object."""

from __future__ import annotations

import time
from dataclasses import asdict, dataclass, field
from typing import TYPE_CHECKING, Any, Generic

from typing_extensions import TypeVar

from prodagent.core.error_classifier import ClassifiedError
from prodagent.core.types import (
    LLMResponse,
    MessageList,
    RunPhase,
    RunState,
    ToolCall,
)

if TYPE_CHECKING:
    from prodagent.core.aliases import JsonDict

CHILD_SEPARATOR = "::"

_TERMINAL_ERROR = "run ended without a terminal event"


def is_child_run_id(run_id: str) -> bool:
    """True when run_id is a child-agent-scoped id (parent::child)."""
    return CHILD_SEPARATOR in run_id


def make_failed_run(run_id: str, task: str, *, last_error: str = _TERMINAL_ERROR) -> AgentRun:
    """Synthetic FAILED run for a stream that ended without a terminal event."""
    return AgentRun(run_id=run_id, task=task, state=RunState.FAILED, last_error=last_error)


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
        return asdict(self)

    @classmethod
    def from_dict(cls, d: JsonDict | None) -> PendingHandoff | None:
        if d is None:
            return None
        if isinstance(d, PendingHandoff):
            return d
        return cls(
            peer_name=d.get("peer_name", ""),
            task=d.get("task", ""),
            input_refs=dict(d.get("input_refs") or {}),
            prior_output=d.get("prior_output", ""),
            peer_run_id=d.get("peer_run_id"),
            message_id=d.get("message_id", ""),
        )


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
        return {
            "turn_count": self.turn_count,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "cache_read_tokens": self.cache_read_tokens,
            "cache_write_tokens": self.cache_write_tokens,
            "cost_usd": self.cost_usd,
        }

    @classmethod
    def from_dict(cls, d: JsonDict | None) -> RunMetrics:
        if d is None:
            return cls()
        return cls(
            turn_count=d.get("turn_count", 0),
            input_tokens=d.get("input_tokens", 0),
            output_tokens=d.get("output_tokens", 0),
            cache_read_tokens=d.get("cache_read_tokens", 0),
            cache_write_tokens=d.get("cache_write_tokens", 0),
            cost_usd=d.get("cost_usd", 0.0),
        )


_RunT = TypeVar("_RunT", default=Any)


@dataclass
class AgentRun(Generic[_RunT]):
    """Central mutable state object for a single agent execution."""

    run_id: str
    task: str
    state: RunState = RunState.RUNNING
    phase: RunPhase = RunPhase.PREPARE

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
    plan_state: JsonDict | None = None
    plan_last_seq: int = 0
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

    def increment_retry(self, tool_name: str) -> int:
        c = self.retry_counter.get(tool_name, 0) + 1
        self.retry_counter[tool_name] = c
        return c

    def reset_retry(self, tool_name: str) -> None:
        self.retry_counter[tool_name] = 0

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
            "run_id": self.run_id,
            "task": self.task,
            "state": self.state.value,
            "phase": self.phase.value,
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
            "plan_state": self.plan_state,
            "plan_last_seq": self.plan_last_seq,
            "is_peer_continuation": self.is_peer_continuation,
        }

    @classmethod
    def from_dict(cls, d: JsonDict) -> AgentRun[Any]:
        return cls(
            run_id=d["run_id"],
            task=d["task"],
            state=RunState(d.get("state", RunState.RUNNING.value)),
            phase=RunPhase(d.get("phase", RunPhase.PREPARE.value)),
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
            plan_state=d.get("plan_state"),
            plan_last_seq=d.get("plan_last_seq", 0),
            is_peer_continuation=d.get("is_peer_continuation", False),
        )
