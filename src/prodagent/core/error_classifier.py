"""Cross-layer error classification: exception -> ClassifiedError."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, replace
from typing import Any

from prodagent.core.error_reason import NON_RETRYABLE_REASONS, ErrorLayer, ErrorReason

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
        return {
            "reason": self.reason.value,
            "code": self.code,
            "retryable": self.retryable,
            "status_code": self.status_code,
            "provider": self.provider,
            "model": self.model,
            "raw_message": self.raw_message,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> ClassifiedError:
        return cls(
            reason=ErrorReason(d["reason"]),
            code=d.get("code", ""),
            retryable=d.get("retryable", True),
            status_code=d.get("status_code"),
            provider=d.get("provider", ""),
            model=d.get("model", ""),
            raw_message=d.get("raw_message", ""),
        )


def _status_code_of(exc: BaseException) -> int | None:
    status: int | None = getattr(exc, "status_code", None)
    if status is not None:
        return status
    response = getattr(exc, "response", None)
    return getattr(response, "status_code", None) if response is not None else None


def _classify_http(exc: BaseException, *, provider: str, model: str) -> ClassifiedError:
    status = _status_code_of(exc)
    message = str(exc)
    reason = _STATUS_TO_REASON.get(status) if status is not None else None

    if reason is None:
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
    classified = _classify_http(exc, provider=provider, model=model)
    message = classified.raw_message.lower()
    if "context_length" in message or "context window" in message or "maximum context" in message:
        return replace(classified, reason=ErrorReason.CONTEXT_OVERFLOW, retryable=False)
    if "content_policy" in message or "content policy" in message or "safety" in message:
        return replace(classified, reason=ErrorReason.CONTENT_BLOCKED, retryable=False)
    return classified


def _classify_runtime(exc: BaseException) -> ClassifiedError:
    """Runtime layer: budget / loop-detected are terminal; everything else
    falls through to the shared timeout / connection / unknown classifier."""
    from prodagent.core.exceptions import BudgetExceeded, InfiniteLoopDetected

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
