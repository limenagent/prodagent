"""Unified error taxonomy shared across LLM, HTTP, tool and runtime layers."""

from __future__ import annotations

from enum import StrEnum


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
