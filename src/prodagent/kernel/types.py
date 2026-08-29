"""The kernel's nouns — one canonical vocabulary for every execution mode.

Calls, responses, results, and stream events live here so both executors
(REACTIVE's Step, PLAN_FIRST's steps) branch on the same shapes. Where
providers disagree, this module picks one canon: ``StopReason`` speaks
Anthropic's vocabulary and every adapter maps into it, unknowns coerced
rather than raised. Words shared with the base layer (``Message``,
``RunState``, ...) are defined in ``base/types`` and re-exported — base
must never import kernel.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import TYPE_CHECKING, Any, Generic, TypeAlias

from typing_extensions import TypeVar

from prodagent.base.codec import dump, load
from prodagent.base.errors import NON_RETRYABLE_REASONS, ErrorReason

# Vocabulary shared with the base layer lives in base/types.py (base must not
# import kernel); re-exported here so kernel consumers keep one import site.
# The redundant `as` aliases mark the re-export explicitly (mypy strict reads it).
from prodagent.base.types import (
    ExecutionMode as ExecutionMode,
)
from prodagent.base.types import (
    Message as Message,
)
from prodagent.base.types import (
    MessageList as MessageList,
)
from prodagent.base.types import (
    RunState as RunState,
)
from prodagent.base.types import (
    stable_serialize as stable_serialize,
)

if TYPE_CHECKING:
    from prodagent.base.types import JsonDict, ToolParams

ToolName: TypeAlias = str
RunId: TypeAlias = str

SKILL_INJECTION_KEY = "_skill_injection"
GET_SKILL_TOOL_NAME = "get_skill"


@dataclass
class ToolCall:
    """Single tool invocation requested by the model.

    Note the vocabulary: ``params`` (not ``args``), and ``call_id`` is the
    correlation key that ties a tool result message back to its request —
    providers reject a tool_result with no matching id."""

    name: ToolName
    params: ToolParams
    call_id: str = ""
    metadata: JsonDict = field(default_factory=dict)

    @property
    def params_hash(self) -> str:
        # Dead-loop detection fingerprints "same tool, same params" with this.
        import hashlib
        import json

        payload = json.dumps(self.params, sort_keys=True, default=stable_serialize)
        return hashlib.sha256(payload.encode()).hexdigest()[:16]

    def to_dict(self) -> JsonDict:
        return dump(self)

    @classmethod
    def from_dict(cls, d: JsonDict) -> ToolCall:
        return load(cls, d, defaults={"params": {}})


class StopReason(StrEnum):
    """Why the model stopped generating. Anthropic's vocabulary is canonical."""

    END_TURN = "end_turn"
    TOOL_USE = "tool_use"
    MAX_TOKENS = "max_tokens"
    CONTENT_FILTER = "content_filter"

    @classmethod
    def coerce(cls, raw: str | StopReason | None) -> StopReason:
        """Best-effort: map unknown strings to END_TURN rather than raising.

        Adapters forward provider-specific values (e.g. OpenAI ``length``,
        GLM ``finish_reason``); callers shouldn't crash on a new one.
        """
        if isinstance(raw, cls):
            return raw  # already canonical
        if not raw:
            return cls.END_TURN  # absent stop reason ≈ model finished
        try:
            return cls(raw)
        except ValueError:
            return cls.END_TURN  # unknown provider value: degrade, never crash


@dataclass
class LLMResponse:
    """Normalised response from any LLM adapter."""

    content: str
    tool_calls: list[ToolCall] = field(default_factory=list)
    stop_reason: StopReason = StopReason.END_TURN
    input_tokens: int = 0
    output_tokens: int = 0
    model: str = ""
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    reasoning_content: str = ""
    """Plain-text view of the model's reasoning (for display, hooks, and
    accounting) — a projection, not the carrier. The raw provider blocks
    live in :attr:`thinking_blocks`."""
    thinking_blocks: list[JsonDict] = field(default_factory=list)
    """Raw reasoning blocks verbatim (Anthropic thinking blocks incl. their
    ``signature``). They ride on the assistant message so a tool-use
    continuation can re-send them — the Anthropic API rejects a tool-result
    turn whose preceding assistant message dropped its thinking blocks."""
    from_cache: bool = False  # skip cost billing when served by CachingLLMClient

    @property
    def total_tokens(self) -> int:
        return self.input_tokens + self.output_tokens

    def to_dict(self) -> JsonDict:
        return {
            "content": self.content,
            "tool_calls": [c.to_dict() for c in self.tool_calls],
            "stop_reason": str(self.stop_reason),
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "cache_read_tokens": self.cache_read_tokens,
            "cache_write_tokens": self.cache_write_tokens,
            "model": self.model,
            "reasoning_content": self.reasoning_content,
            "thinking_blocks": [dict(b) for b in self.thinking_blocks],
        }

    @classmethod
    def from_dict(cls, d: JsonDict) -> LLMResponse:
        return cls(
            content=d.get("content", ""),
            tool_calls=[ToolCall.from_dict(c) for c in d.get("tool_calls", [])],
            stop_reason=StopReason.coerce(d.get("stop_reason", "end_turn")),
            input_tokens=d.get("input_tokens", 0),
            output_tokens=d.get("output_tokens", 0),
            cache_read_tokens=d.get("cache_read_tokens", 0),
            cache_write_tokens=d.get("cache_write_tokens", 0),
            model=d.get("model", ""),
            reasoning_content=d.get("reasoning_content", ""),
            thinking_blocks=[dict(b) for b in d.get("thinking_blocks", [])],
        )


class StepStatus(StrEnum):
    # OBSOLETE is distinct from FAILED: replanning can invalidate a pending
    # step through no fault of its own — it neither ran nor failed, so resume
    # logic must not treat it as either.
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    OBSOLETE = "obsolete"
    SUSPENDED = "suspended"


class SideEffectLevel(StrEnum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


@dataclass
class ToolMeta:
    """Static metadata the Loop uses to make policy decisions."""

    name: ToolName
    is_readonly: bool = False
    side_effect_level: SideEffectLevel = SideEffectLevel.LOW
    enforced_idempotent: bool = False  # host injects idempotency_key; tool fn must accept it
    timeout_seconds: float = 10.0
    """Hard deadline — the dispatcher enforces it with ``asyncio.wait_for``.
    A deadline is a correctness bound, not a forecast; set it from p99.9 of
    the tool's real runtime, not from what you *expect* it to take."""
    domain: str = "general"
    max_result_chars: float = 100_000

    def __post_init__(self) -> None:
        if self.timeout_seconds <= 0:
            raise ValueError(
                f"ToolMeta {self.name!r}: timeout_seconds must be > 0 (got {self.timeout_seconds})"
            )


class ErrorSeverity(StrEnum):
    RED = "red"
    YELLOW = "yellow"

    @classmethod
    def coerce(
        cls, raw: str | ErrorSeverity | None, default: ErrorSeverity | None = None
    ) -> ErrorSeverity:
        """Best-effort: map unknown strings to ``default`` (RED if unset) rather than raising."""
        fallback = default if default is not None else cls.RED
        if isinstance(raw, cls):
            return raw
        if not raw:
            return fallback
        try:
            return cls(raw)
        except ValueError:
            return fallback


@dataclass
class ToolError:
    """User-facing helper for returning structured errors from tools.

    `reason` is the controlled vocabulary driving retry/severity decisions;
    `code` is a free-form identifier for logging/messaging — see
    prodagent.base.errors for the shared severity default table.
    """

    reason: ErrorReason
    code: str
    error_severity: ErrorSeverity
    message: str = ""
    hint: str = ""

    @classmethod
    def from_reason(
        cls,
        reason: ErrorReason,
        *,
        code: str = "",
        message: str = "",
        hint: str = "",
        severity: ErrorSeverity | None = None,
    ) -> ToolError:
        """Convenience constructor that derives severity from the reason via
        the NON_RETRYABLE table — callers state *what* failed, the shared
        table decides how the loop should treat it."""
        if severity is None:
            severity = (
                ErrorSeverity.RED if reason in NON_RETRYABLE_REASONS else ErrorSeverity.YELLOW
            )
        return cls(
            reason=reason,
            code=code or reason.value,
            error_severity=severity,
            message=message or code or reason.value,
            hint=hint,
        )

    def as_dict(self) -> JsonDict:
        return {
            "error": True,
            "reason": self.reason.value,
            "code": self.code,
            "error_severity": self.error_severity.value,
            "message": self.message,
            "hint": self.hint,
        }


class ToolOutcome(StrEnum):
    """Coarse classification the Loop switches on after a tool returns."""

    OK = "ok"
    RETRY = "retry"
    ABORT = "abort"
    BLOCKED = "blocked"
    SUSPENDED = "suspended"  # run paused (e.g. HITL approval) — halt the loop, never retry
    HANDOFF = "handoff"  # transfer control to a peer agent — run COMPLETED


_T = TypeVar("_T", default=Any)


@dataclass(frozen=True)
class ToolResult(Generic[_T]):
    """Typed envelope for a single tool invocation's result."""

    outcome: ToolOutcome
    value: _T | None = None
    error: ToolError | None = None
    reason: str = ""
    tool: ToolName = ""
    approval_request_id: str = ""  # populated on SUSPENDED — correlates to submit_decision
    handoff: JsonDict | None = None  # populated on HANDOFF — {peer, task, input_refs}

    @classmethod
    def from_error(cls, err: ToolError, *, tool: ToolName = "") -> ToolResult[Any]:
        outcome = (
            ToolOutcome.RETRY if err.error_severity is ErrorSeverity.YELLOW else ToolOutcome.ABORT
        )
        return cls(outcome, error=err, tool=tool)

    @classmethod
    def blocked_by(cls, reason: str, *, tool: ToolName = "") -> ToolResult[Any]:
        return cls(ToolOutcome.BLOCKED, reason=reason, tool=tool)

    @classmethod
    def suspended(
        cls,
        *,
        reason: str = "",
        tool: ToolName = "",
        approval_request_id: str = "",
    ) -> ToolResult[Any]:
        return cls(
            ToolOutcome.SUSPENDED,
            reason=reason,
            tool=tool,
            approval_request_id=approval_request_id,
        )

    @classmethod
    def for_handoff(
        cls,
        *,
        peer: str,
        task: str,
        input_refs: dict[str, str] | None = None,
        tool: ToolName = "",
    ) -> ToolResult[Any]:
        return cls(
            ToolOutcome.HANDOFF,
            tool=tool,
            handoff={"peer": peer, "task": task, "input_refs": input_refs or {}},
        )

    def to_wire(self) -> JsonDict:
        """The dict the model actually reads as a tool result. One shape per
        outcome — errors carry reason/hint for self-correction, suspensions
        carry the approval id, handoffs carry the peer; a plain ``OK`` value
        passes through as itself when it's already a dict."""
        if self.outcome is ToolOutcome.OK:
            v = self.value
            return v if isinstance(v, dict) else {"result": v}
        if self.outcome is ToolOutcome.BLOCKED:
            wire = {"blocked": True, "reason": self.reason}
            if self.tool:
                wire["tool"] = self.tool
            return wire
        if self.outcome is ToolOutcome.SUSPENDED:
            wire = {"suspended": True, "reason": self.reason}
            if self.tool:
                wire["tool"] = self.tool
            if self.approval_request_id:
                wire["approval_request_id"] = self.approval_request_id
            return wire
        if self.outcome is ToolOutcome.HANDOFF:
            wire = {"handoff": True, "peer": self.handoff.get("peer", "") if self.handoff else ""}
            if self.tool:
                wire["tool"] = self.tool
            return wire
        assert self.error is not None
        return self.error.as_dict()


# ── Streaming events — the run-stream union lives in ports (wire vocabulary) ──
# Lifted to prodagent.ports.agent_events: they are every stream consumer's
# contract (LeafExecutor / RunnerPort / the remote plane) and carry a wire
# codec there. Re-exported here so kernel consumers keep one import site —
# same precedent as the base-vocabulary re-exports at the top of this module.
# The redundant `as` aliases mark the re-export explicitly (mypy strict).

from prodagent.ports.agent_events import (  # noqa: E402
    AgentEvent as AgentEvent,
)
from prodagent.ports.agent_events import (  # noqa: E402
    RunCompletedEvent as RunCompletedEvent,
)
from prodagent.ports.agent_events import (  # noqa: E402
    RunFailedEvent as RunFailedEvent,
)
from prodagent.ports.agent_events import (  # noqa: E402
    RunSuspendedEvent as RunSuspendedEvent,
)
from prodagent.ports.agent_events import (  # noqa: E402
    StepCompletedEvent as StepCompletedEvent,
)
from prodagent.ports.agent_events import (  # noqa: E402
    StepFailedEvent as StepFailedEvent,
)
from prodagent.ports.agent_events import (  # noqa: E402
    StepStartedEvent as StepStartedEvent,
)
from prodagent.ports.agent_events import (  # noqa: E402
    ThinkTokenEvent as ThinkTokenEvent,
)
from prodagent.ports.agent_events import (  # noqa: E402
    ToolCallStartEvent as ToolCallStartEvent,
)
from prodagent.ports.agent_events import (  # noqa: E402
    ToolResultEvent as ToolResultEvent,
)
