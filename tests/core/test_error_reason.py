from __future__ import annotations

from prodagent.core.error_classifier import NON_RETRYABLE_REASONS
from prodagent.core.error_reason import ErrorLayer, ErrorReason

_EXPECTED_VALUES = {
    "auth_invalid",
    "auth_forbidden",
    "rate_limited",
    "quota_exhausted",
    "billing",
    "overloaded",
    "server_error",
    "timeout",
    "connection",
    "context_overflow",
    "payload_too_large",
    "content_blocked",
    "model_not_found",
    "format_error",
    "budget_exceeded",
    "runtime_loop_detected",
    "tool_not_available",
    "unknown",
}

_EXPECTED_NON_RETRYABLE = {
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


def test_error_reason_has_exactly_18_values():
    assert {r.value for r in ErrorReason} == _EXPECTED_VALUES
    assert len(ErrorReason) == 18


def test_non_retryable_reasons_table_is_exhaustive_and_correct():
    assert NON_RETRYABLE_REASONS == _EXPECTED_NON_RETRYABLE


def test_retryable_reasons_are_every_reason_not_in_non_retryable_table():
    retryable = set(ErrorReason) - NON_RETRYABLE_REASONS
    assert retryable == {
        ErrorReason.RATE_LIMITED,
        ErrorReason.OVERLOADED,
        ErrorReason.SERVER_ERROR,
        ErrorReason.TIMEOUT,
        ErrorReason.CONNECTION,
        ErrorReason.CONTEXT_OVERFLOW,
        ErrorReason.PAYLOAD_TOO_LARGE,
    }


def test_error_layer_has_4_values():
    assert {layer.value for layer in ErrorLayer} == {"llm", "tool", "http", "runtime"}
