"""The error model — one concept, three facets.

Everything the framework says about failure lives in this module:

1. **Reason vocabulary** (:class:`ErrorReason` / :class:`ErrorLayer`) — the
   controlled vocabulary driving retry / severity / recovery decisions;
2. **Exception tree** (:class:`AgentError` and friends) — the raise surface;
3. **Classifier** (:func:`classify_error` → :class:`ClassifiedError`) —
   mapping any raised exception back onto the vocabulary.

Three modules used to carry these facets; they are one concept, so they live
together — adding a failure mode means editing exactly one file.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, replace
from enum import StrEnum
from typing import Any

from prodagent.base.codec import dump, load

# ── Reason vocabulary ─────────────────────────────────────────────────────────


class ErrorReason(StrEnum):
    """Controlled vocabulary driving retry / severity / recovery-hint decisions."""

    # auth
    AUTH_INVALID = "auth_invalid"
    AUTH_FORBIDDEN = "auth_forbidden"
    # quota / billing
    RATE_LIMITED = "rate_limited"
    QUOTA_EXHAUSTED = "quota_exhausted"
    BILLING = "billing"
    # server
    OVERLOADED = "overloaded"
    SERVER_ERROR = "server_error"
    # transport
    TIMEOUT = "timeout"
    CONNECTION = "connection"
    # payload / content
    CONTEXT_OVERFLOW = "context_overflow"
    PAYLOAD_TOO_LARGE = "payload_too_large"
    CONTENT_BLOCKED = "content_blocked"
    MODEL_NOT_FOUND = "model_not_found"
    FORMAT_ERROR = "format_error"
    # runtime
    BUDGET_EXCEEDED = "budget_exceeded"
    RUNTIME_LOOP_DETECTED = "runtime_loop_detected"
    TOOL_NOT_AVAILABLE = "tool_not_available"
    RESOURCE_BUSY = "resource_busy"
    # catch-all
    UNKNOWN = "unknown"


class ErrorLayer(StrEnum):
    """Which subsystem produced the exception being classified."""

    LLM = "llm"
    TOOL = "tool"
    HTTP = "http"
    RUNTIME = "runtime"


NON_RETRYABLE_REASONS: frozenset[ErrorReason] = frozenset(
    # Absence from this set means "presumed transient". Retrying a permanent
    # failure only burns budget and delays the user-visible failure, so the
    # burden of proof is on listing a reason as retryable.
    {
        ErrorReason.AUTH_INVALID,
        ErrorReason.AUTH_FORBIDDEN,
        ErrorReason.BILLING,
        ErrorReason.QUOTA_EXHAUSTED,
        ErrorReason.CONTENT_BLOCKED,
        ErrorReason.MODEL_NOT_FOUND,
        ErrorReason.FORMAT_ERROR,
        ErrorReason.BUDGET_EXCEEDED,
        ErrorReason.RUNTIME_LOOP_DETECTED,
        ErrorReason.TOOL_NOT_AVAILABLE,
        ErrorReason.UNKNOWN,
    }
)


# ── Exception tree ───────────────────────────────────────────────────────────


class AgentError(Exception):
    """Root of the exception tree."""

    def __init__(self, message: str, **context: Any) -> None:
        super().__init__(message)
        self.message = message
        self.context: dict[str, Any] = context


class BudgetExceeded(AgentError):
    """Hard budget (turns / seconds / tokens / USD) exhausted."""


class InfiniteLoopDetected(AgentError):
    """Same tool-call fingerprint repeated beyond threshold."""


class ToolAbortError(AgentError):
    """Tool returned ABORT — the tool itself signalled failure."""


class ToolBlockedError(AgentError):
    """Tool returned BLOCKED — HITL approval was denied."""


class PermissionDenied(AgentError):
    """Tool call not permitted for this agent or session."""


class PromptInjectionDetected(AgentError):
    """Input contains prompt-injection payload; request quarantined."""


class SensitiveContentDetected(AgentError):
    """Output contains sensitive content (PII/secret) per the app's policy; vetoed."""


class SuspendPendingApproval(AgentError):
    """Execution suspended pending human approval.

    Carries request_id so the resuming caller can correlate with submit_decision.
    """

    def __init__(
        self,
        message: str,
        *,
        tool: str = "",
        request_id: str = "",
        **context: Any,
    ) -> None:
        super().__init__(message, tool=tool, request_id=request_id, **context)
        self.tool = tool
        self.request_id = request_id


class SecurityViolation(AgentError):
    """Generic security violation."""


class VersionConflict(AgentError):
    """Optimistic-concurrency violation on the append-only event log."""


class CorruptedCheckpointError(AgentError):
    """Checkpoint JSON unparseable or fails schema validation."""


class UnknownApprovalError(AgentError):
    """Decision submitted for a non-existent approval request."""


class PlanAlreadyCompletedError(AgentError):
    """A workflow agent's preset Plan was already executed in a prior turn."""

    def __init__(self, run_id: str) -> None:
        super().__init__(
            f"workflow plan for run={run_id} already completed — "
            "preset Plan runs once; use mode='reactive' or mode='plan_first' for further turns"
        )
        self.run_id = run_id


class RunIdCollisionError(AgentError):
    """A freshly minted run_id already has a checkpoint in the store."""

    def __init__(self, run_id: str) -> None:
        super().__init__(
            f"run_id={run_id} already has a checkpoint — refusing to mint a "
            "new turn over an orphan checkpoint; resolve the existing run first"
        )
        self.run_id = run_id


class ToolCallParseError(AgentError):
    """LLM returned a tool call whose arguments were not valid JSON."""


class LLMError(AgentError):
    """LLM call failed (network, auth, rate-limit, malformed response, etc.)."""


SECURITY_VETO_EXCEPTIONS: tuple[type[BaseException], ...] = (
    # The one family that must never be swallowed as "tool failed, carry on":
    # wherever these surface (dispatchers, pipelines, retry loops), they
    # re-raise — a security stop outranks graceful degradation.
    PermissionDenied,
    PromptInjectionDetected,
    SensitiveContentDetected,
    SecurityViolation,
    SuspendPendingApproval,
)


# ── Classifier: exception -> ClassifiedError ─────────────────────────────────


PERMANENT_STATUS_CODES: frozenset[int] = frozenset({400, 401, 402, 403, 404, 422})
RETRYABLE_STATUS_CODES: frozenset[int] = frozenset({408, 429, 500, 502, 503, 504, 529})

_STATUS_TO_REASON: dict[int, ErrorReason] = {
    400: ErrorReason.FORMAT_ERROR,
    422: ErrorReason.FORMAT_ERROR,
    401: ErrorReason.AUTH_INVALID,
    402: ErrorReason.BILLING,
    403: ErrorReason.AUTH_FORBIDDEN,
    404: ErrorReason.MODEL_NOT_FOUND,
    408: ErrorReason.TIMEOUT,
    500: ErrorReason.SERVER_ERROR,
    502: ErrorReason.SERVER_ERROR,
    504: ErrorReason.SERVER_ERROR,
    503: ErrorReason.OVERLOADED,
    529: ErrorReason.OVERLOADED,
}


@dataclass
class ClassifiedError:
    """Result of classifying an exception: what happened + how to recover."""

    reason: ErrorReason
    code: str = ""  # tool business errors fill this; LLM/HTTP/Runtime default to reason.value
    retryable: bool = True
    status_code: int | None = None
    provider: str = ""
    model: str = ""
    raw_message: str = ""

    def to_dict(self) -> dict[str, Any]:
        return dump(self)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> ClassifiedError:
        return load(cls, d)


def _status_code_of(exc: BaseException) -> int | None:
    """Dig a status out of either shape providers throw — on the exception
    itself, or on its ``.response``. Duck-typing keeps the classifier free
    of any SDK import."""
    status: int | None = getattr(exc, "status_code", None)
    if status is not None:
        return status
    response = getattr(exc, "response", None)
    return getattr(response, "status_code", None) if response is not None else None


def _classify_http(exc: BaseException, *, provider: str, model: str) -> ClassifiedError:
    """Map a transport/HTTP exception onto the vocabulary by status code,
    falling back to exception type (timeout / connection), then UNKNOWN —
    which stays retryable, per the presumed-transient default."""
    status = _status_code_of(exc)
    message = str(exc)
    reason = _STATUS_TO_REASON.get(status) if status is not None else None

    if reason is None:
        # 429 is ambiguous at the protocol level: quota means stop for good,
        # rate limit means back off — so the message body disambiguates.
        if status == 429:
            reason = (
                ErrorReason.QUOTA_EXHAUSTED
                if "quota" in message.lower()
                else ErrorReason.RATE_LIMITED
            )
        elif isinstance(exc, (TimeoutError, asyncio.TimeoutError)):
            reason = ErrorReason.TIMEOUT
        elif isinstance(exc, (ConnectionError, OSError)):
            reason = ErrorReason.CONNECTION
        else:
            reason = ErrorReason.UNKNOWN

    return ClassifiedError(
        reason=reason,
        retryable=reason not in NON_RETRYABLE_REASONS,
        status_code=status,
        provider=provider,
        model=model,
        raw_message=message,
    )


def _classify_llm(exc: BaseException, *, provider: str, model: str) -> ClassifiedError:
    """HTTP classification, then provider-message refinement: context-window
    and content-policy failures don't map to status codes — every provider
    reports them in prose, so the message body is the signal."""
    classified = _classify_http(exc, provider=provider, model=model)
    message = classified.raw_message.lower()
    # Prose sniffing: these two failure families carry no status code — every
    # provider reports them in the message body, so the body is the signal.
    if "context_length" in message or "context window" in message or "maximum context" in message:
        return replace(
            classified, reason=ErrorReason.CONTEXT_OVERFLOW, retryable=False
        )  # unfixable by retry
    if "content_policy" in message or "content policy" in message or "safety" in message:
        return replace(
            classified, reason=ErrorReason.CONTENT_BLOCKED, retryable=False
        )  # policy, not fault
    return classified


def _classify_runtime(exc: BaseException) -> ClassifiedError:
    """Runtime layer: budget / loop-detected are terminal; everything else
    falls through to the shared timeout / connection / unknown classifier."""
    if isinstance(exc, BudgetExceeded):
        return ClassifiedError(
            reason=ErrorReason.BUDGET_EXCEEDED,
            retryable=False,
            raw_message=str(exc),
        )
    if isinstance(exc, InfiniteLoopDetected):
        return ClassifiedError(
            reason=ErrorReason.RUNTIME_LOOP_DETECTED,
            retryable=False,
            raw_message=str(exc),
        )
    return _classify_transport(exc)


def _classify_transport(exc: BaseException) -> ClassifiedError:
    """Timeout / connection / unknown — shared by runtime and tool layers."""
    if isinstance(exc, TimeoutError):
        reason = ErrorReason.TIMEOUT
    elif isinstance(exc, (ConnectionError, OSError)):
        reason = ErrorReason.CONNECTION
    else:
        reason = ErrorReason.UNKNOWN
    return ClassifiedError(
        reason=reason, retryable=reason not in NON_RETRYABLE_REASONS, raw_message=str(exc)
    )


def classify_error(
    exc: BaseException,
    *,
    layer: ErrorLayer,
    provider: str = "",
    model: str = "",
) -> ClassifiedError:
    """Classify *exc* raised at *layer* into a reason + recovery hints."""
    if layer is ErrorLayer.HTTP:
        return _classify_http(exc, provider=provider, model=model)
    if layer is ErrorLayer.LLM:
        return _classify_llm(exc, provider=provider, model=model)
    if layer is ErrorLayer.RUNTIME:
        return _classify_runtime(exc)
    if layer is ErrorLayer.TOOL:
        return _classify_transport(exc)
    raise ValueError(f"unknown error layer: {layer!r}")
