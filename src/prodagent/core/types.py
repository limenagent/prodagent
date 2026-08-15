"""Shared type definitions."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import TYPE_CHECKING, Any, Generic, Literal, TypeAlias, TypedDict

from typing_extensions import TypeVar

from prodagent.core.error_reason import NON_RETRYABLE_REASONS, ErrorReason

if TYPE_CHECKING:
    from prodagent.core.aliases import JsonDict, ToolParams

ToolName: TypeAlias = str
RunId: TypeAlias = str

SKILL_INJECTION_KEY = "_skill_injection"
GET_SKILL_TOOL_NAME = "get_skill"


class Message(TypedDict, total=False):
    """Strongly-typed LLM conversation message."""

    role: Literal["user", "assistant", "system", "tool"]
    content: str | list[JsonDict]
    tool_calls: list[JsonDict]
    tool_call_id: str


MessageList: TypeAlias = list[Message]


def stable_serialize(obj: object) -> object:
    """Best-effort stable JSON pre-serializer for fingerprint / hash computation."""
    import datetime
    import decimal
    import pathlib

    if isinstance(obj, (datetime.datetime, datetime.date, datetime.time)):
        return obj.isoformat()
    if isinstance(obj, datetime.timedelta):
        return repr(obj)
    if isinstance(obj, decimal.Decimal):
        return str(obj)
    if isinstance(obj, pathlib.PurePath):
        return str(obj)
    return repr(obj)


@dataclass
class ToolCall:
    """Single tool invocation requested by the model."""

    name: ToolName
    params: ToolParams
    call_id: str = ""
    metadata: JsonDict = field(default_factory=dict)

    @property
    def params_hash(self) -> str:
        import hashlib
        import json

        payload = json.dumps(self.params, sort_keys=True, default=stable_serialize)
        return hashlib.sha256(payload.encode()).hexdigest()[:16]

    def to_dict(self) -> JsonDict:
        return {
            "name": self.name,
            "params": self.params,
            "call_id": self.call_id,
            "metadata": dict(self.metadata) if self.metadata else {},
        }

    @classmethod
    def from_dict(cls, d: JsonDict) -> ToolCall:
        return cls(
            name=d["name"],
            params=d.get("params", {}),
            call_id=d.get("call_id", ""),
            metadata=d.get("metadata", {}) or {},
        )


class StopReason(StrEnum):
    """Why the model stopped generating. Anthropic's vocabulary is canonical."""

    END_TURN = "end_turn"
    TOOL_USE = "tool_use"
    MAX_TOKENS = "max_tokens"
    STOP_SEQUENCE = "stop_sequence"
    CONTENT_FILTER = "content_filter"

    @classmethod
    def coerce(cls, raw: str | StopReason | None) -> StopReason:
        """Best-effort: map unknown strings to END_TURN rather than raising.

        Adapters forward provider-specific values (e.g. OpenAI ``length``,
        GLM ``finish_reason``); callers shouldn't crash on a new one.
        """
        if isinstance(raw, cls):
            return raw
        if not raw:
            return cls.END_TURN
        try:
            return cls(raw)
        except ValueError:
            return cls.END_TURN


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
        )


class RunState(StrEnum):
    RUNNING = "running"
    SUSPENDED = "suspended"
    COMPLETED = "completed"
    FAILED = "failed"


class StepStatus(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    OBSOLETE = "obsolete"
    SUSPENDED = "suspended"


class RunPhase(StrEnum):
    PREPARE = "prepare"
    THINK = "think"
    DECIDE = "decide"
    EXECUTE = "execute"
    DONE = "done"


class Layer(StrEnum):
    L0 = "L0"
    L1 = "L1"
    L2 = "L2"
    L3 = "L3"


class ExecutionMode(StrEnum):
    """Controls how an Agent decides which tools to call and in what order."""

    PLAN_FIRST = "plan_first"
    """LLM proposes a full execution plan (JSON DAG); framework executes it.
    Enables plan auditing and HITL plan review. Default."""

    REACTIVE = "reactive"
    """Each turn picks the next tool based on the previous result.
    WARNING: bypasses plan auditing and HITL plan review."""


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
    estimated_latency_ms: float = 1_000.0
    domain: str = "general"
    resource_id: str | None = None
    max_result_chars: float = 100_000

    @property
    def timeout_seconds(self) -> float:
        return self.estimated_latency_ms / 1_000


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
    prodagent.core.error_classifier for the shared severity default table.
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

    @classmethod
    def from_raw(cls, raw: Any, *, tool: ToolName = "") -> ToolResult[Any]:
        if isinstance(raw, ToolResult):
            return raw
        if isinstance(raw, ToolError):
            return cls.from_error(raw, tool=tool)
        if isinstance(raw, dict):
            if raw.get("suspended"):
                return cls.suspended(
                    reason=raw.get("reason", ""),
                    tool=raw.get("tool", tool),
                    approval_request_id=raw.get("approval_request_id", ""),
                )
            if raw.get("handoff"):
                return cls.for_handoff(
                    peer=raw.get("peer", ""),
                    task=raw.get("task", ""),
                    input_refs=raw.get("input_refs"),
                    tool=raw.get("tool", tool),
                )
            if raw.get("blocked"):
                return cls.blocked_by(raw.get("reason", ""), tool=raw.get("tool", tool))
            if raw.get("error"):
                raw_reason = raw.get("reason", "")
                err_val = raw.get("error")
                message = raw.get("message", "")
                if isinstance(err_val, str) and not message:
                    message = err_val
                try:
                    reason = ErrorReason(raw_reason)
                except ValueError:
                    reason = ErrorReason.UNKNOWN
                    message = message or f"invalid ErrorReason: {raw_reason!r}"
                return cls.from_error(
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
        return cls(ToolOutcome.OK, value=raw, tool=tool)

    def to_wire(self) -> JsonDict:
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
