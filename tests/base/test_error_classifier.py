from __future__ import annotations

import pytest

from prodagent.base.errors import (
    BudgetExceeded,
    ErrorLayer,
    ErrorReason,
    InfiniteLoopDetected,
    classify_error,
)


class _FakeResponse:
    def __init__(self, status_code: int) -> None:
        self.status_code = status_code
        self.headers: dict[str, str] = {}


class _FakeHttpError(Exception):
    def __init__(self, message: str, status_code: int) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.response = _FakeResponse(status_code)


class TestClassifyHttp:
    def test_401_is_auth_invalid_non_retryable(self):
        classified = classify_error(_FakeHttpError("unauthorized", 401), layer=ErrorLayer.HTTP)
        assert classified.reason is ErrorReason.AUTH_INVALID
        assert classified.retryable is False
        assert classified.status_code == 401

    def test_402_is_billing_non_retryable(self):
        classified = classify_error(_FakeHttpError("payment required", 402), layer=ErrorLayer.HTTP)
        assert classified.reason is ErrorReason.BILLING
        assert classified.retryable is False

    def test_429_is_rate_limited_retryable(self):
        classified = classify_error(_FakeHttpError("too many requests", 429), layer=ErrorLayer.HTTP)
        assert classified.reason is ErrorReason.RATE_LIMITED
        assert classified.retryable is True

    def test_429_with_quota_message_is_quota_exhausted(self):
        classified = classify_error(_FakeHttpError("quota exceeded", 429), layer=ErrorLayer.HTTP)
        assert classified.reason is ErrorReason.QUOTA_EXHAUSTED
        assert classified.retryable is False

    def test_529_is_overloaded_retryable(self):
        classified = classify_error(_FakeHttpError("overloaded", 529), layer=ErrorLayer.HTTP)
        assert classified.reason is ErrorReason.OVERLOADED
        assert classified.retryable is True

    def test_404_is_model_not_found(self):
        classified = classify_error(_FakeHttpError("not found", 404), layer=ErrorLayer.HTTP)
        assert classified.reason is ErrorReason.MODEL_NOT_FOUND
        assert classified.retryable is False


class TestClassifyLlm:
    def test_context_length_message_sets_context_overflow(self):
        exc = _FakeHttpError("maximum context length exceeded", 400)
        classified = classify_error(exc, layer=ErrorLayer.LLM)
        assert classified.reason is ErrorReason.CONTEXT_OVERFLOW
        assert classified.retryable is False

    def test_content_policy_message_sets_content_blocked(self):
        exc = _FakeHttpError("blocked by content policy", 400)
        classified = classify_error(exc, layer=ErrorLayer.LLM)
        assert classified.reason is ErrorReason.CONTENT_BLOCKED
        assert classified.retryable is False

    def test_plain_429_falls_through_to_http_classification(self):
        classified = classify_error(_FakeHttpError("rate limited", 429), layer=ErrorLayer.LLM)
        assert classified.reason is ErrorReason.RATE_LIMITED
        assert classified.retryable is True


class TestClassifyRuntime:
    def test_budget_exceeded_is_non_retryable(self):
        exc = BudgetExceeded("boom", run_id="r1", axis="seconds", value=10, limit=5)
        classified = classify_error(exc, layer=ErrorLayer.RUNTIME)
        assert classified.reason is ErrorReason.BUDGET_EXCEEDED
        assert classified.retryable is False

    def test_infinite_loop_detected_is_non_retryable(self):
        exc = InfiniteLoopDetected("looping")
        classified = classify_error(exc, layer=ErrorLayer.RUNTIME)
        assert classified.reason is ErrorReason.RUNTIME_LOOP_DETECTED
        assert classified.retryable is False

    def test_connection_error_is_retryable(self):
        classified = classify_error(ConnectionError("down"), layer=ErrorLayer.RUNTIME)
        assert classified.reason is ErrorReason.CONNECTION
        assert classified.retryable is True

    def test_generic_exception_is_unknown_non_retryable(self):
        classified = classify_error(ValueError("mystery"), layer=ErrorLayer.RUNTIME)
        assert classified.reason is ErrorReason.UNKNOWN
        assert classified.retryable is False


class TestClassifyTool:
    def test_timeout_error_is_timeout_retryable(self):
        classified = classify_error(TimeoutError("slow"), layer=ErrorLayer.TOOL)
        assert classified.reason is ErrorReason.TIMEOUT
        assert classified.retryable is True

    def test_connection_error_is_connection_retryable(self):
        classified = classify_error(ConnectionError("down"), layer=ErrorLayer.TOOL)
        assert classified.reason is ErrorReason.CONNECTION
        assert classified.retryable is True

    def test_generic_exception_is_unknown_non_retryable(self):
        classified = classify_error(RuntimeError("oops"), layer=ErrorLayer.TOOL)
        assert classified.reason is ErrorReason.UNKNOWN
        assert classified.retryable is False


def test_classify_error_rejects_unknown_layer():
    with pytest.raises(ValueError):
        classify_error(ValueError("x"), layer="bogus")  # type: ignore[arg-type]


def test_classified_error_round_trips_through_dict():
    classified = classify_error(
        BudgetExceeded("boom", run_id="r1", axis="seconds", value=10, limit=5),
        layer=ErrorLayer.RUNTIME,
    )
    restored = type(classified).from_dict(classified.to_dict())
    assert restored == classified
