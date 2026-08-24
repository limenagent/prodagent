from __future__ import annotations

import pytest

from prodagent.core.error_reason import ErrorReason
from prodagent.core.retry import Backoff, RetryPolicy
from prodagent.llm.http_retry import (
    CapacityError,
    _delay_for,
    with_http_retry,
)


class _FakeHeaders(dict):
    pass


class _FakeResponse:
    def __init__(self, status_code: int, retry_after: str | None = None) -> None:
        self.status_code = status_code
        self.headers = _FakeHeaders()
        if retry_after is not None:
            self.headers["retry-after"] = retry_after


class _FakeApiStatusError(Exception):
    def __init__(self, message: str, status_code: int, retry_after: str | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.response = _FakeResponse(status_code, retry_after)


class TestDelayFor:
    def test_retry_after_header_takes_priority(self) -> None:
        policy = RetryPolicy(max_attempts=5, base_delay=1.0, backoff=Backoff.EXPONENTIAL)
        assert _delay_for(attempt=3, retry_after_header="5", policy=policy) == 5.0

    def test_retry_after_header_float(self) -> None:
        policy = RetryPolicy()
        assert _delay_for(attempt=0, retry_after_header="2.5", policy=policy) == 2.5

    def test_retry_after_header_capped_at_max_delay(self) -> None:
        policy = RetryPolicy(
            max_attempts=5, base_delay=1.0, max_delay=30.0, backoff=Backoff.EXPONENTIAL
        )
        assert _delay_for(attempt=3, retry_after_header="86400", policy=policy) == 30.0

    def test_retry_after_header_negative_clamped(self) -> None:
        policy = RetryPolicy(max_attempts=5, base_delay=1.0, max_delay=60.0)
        assert _delay_for(attempt=1, retry_after_header="-5", policy=policy) == 0.0

    def test_retry_after_header_non_numeric_falls_back_to_backoff(self) -> None:
        policy = RetryPolicy(base_delay=1.0, backoff=Backoff.EXPONENTIAL)
        delay = _delay_for(
            attempt=1, retry_after_header="Wed, 21 Oct 2015 07:28:00 GMT", policy=policy
        )
        assert delay >= 1.0

    def test_exponential_backoff_attempt_1(self) -> None:
        policy = RetryPolicy(base_delay=1.0, max_delay=60.0, backoff=Backoff.EXPONENTIAL)
        assert _delay_for(attempt=1, retry_after_header=None, policy=policy) == 1.0

    def test_exponential_backoff_attempt_4(self) -> None:
        policy = RetryPolicy(base_delay=1.0, max_delay=60.0, backoff=Backoff.EXPONENTIAL)
        assert _delay_for(attempt=4, retry_after_header=None, policy=policy) == 8.0

    def test_backoff_capped_at_max(self) -> None:
        policy = RetryPolicy(base_delay=1.0, max_delay=32.0, backoff=Backoff.EXPONENTIAL)
        assert _delay_for(attempt=10, retry_after_header=None, policy=policy) == 32.0

    def test_jittered_within_bounds(self) -> None:
        policy = RetryPolicy(base_delay=1.0, max_delay=32.0, backoff=Backoff.JITTERED)
        for _ in range(20):
            delay = _delay_for(attempt=3, retry_after_header=None, policy=policy)
            assert 0.0 <= delay <= 4.0

    def test_fixed_backoff(self) -> None:
        policy = RetryPolicy(base_delay=0.5, backoff=Backoff.FIXED)
        for attempt in (1, 2, 5, 10):
            assert _delay_for(attempt=attempt, retry_after_header=None, policy=policy) == 0.5


class TestWithLlmRetryHappyPath:
    async def test_first_attempt_success(self) -> None:
        call_count = 0

        async def factory() -> str:
            nonlocal call_count
            call_count += 1
            return "ok"

        result = await with_http_retry(factory, policy=RetryPolicy(max_attempts=3))
        assert result == "ok"
        assert call_count == 1

    async def test_second_attempt_succeeds_after_429(self) -> None:
        call_count = 0

        async def factory() -> str:
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise _FakeApiStatusError("rate limited", status_code=429)
            return "ok"

        policy = RetryPolicy(max_attempts=3, base_delay=0.001, backoff=Backoff.FIXED)
        result = await with_http_retry(factory, policy=policy)
        assert result == "ok"
        assert call_count == 2

    async def test_third_attempt_succeeds_after_503(self) -> None:
        call_count = 0

        async def factory() -> str:
            nonlocal call_count
            call_count += 1
            if call_count < 3:
                raise _FakeApiStatusError("service unavailable", status_code=503)
            return "ok"

        policy = RetryPolicy(max_attempts=5, base_delay=0.001, backoff=Backoff.FIXED)
        result = await with_http_retry(factory, policy=policy)
        assert result == "ok"
        assert call_count == 3


class TestWithLlmRetryPermanentErrors:
    @pytest.mark.parametrize("status", [400, 401, 402, 403, 404, 422])
    async def test_permanent_status_codes_raise_immediately(self, status: int) -> None:
        call_count = 0

        async def factory() -> str:
            nonlocal call_count
            call_count += 1
            raise _FakeApiStatusError(f"HTTP {status}", status_code=status)

        with pytest.raises(_FakeApiStatusError):
            await with_http_retry(factory, policy=RetryPolicy(max_attempts=5))
        assert call_count == 1


class TestHttpRetryClassifiesException:
    """HTTP error classification is unit-tested directly on ``classify_error``;
    the retry path just needs to raise the right exception type."""

    @pytest.mark.parametrize(
        ("status", "reason"),
        [
            (401, ErrorReason.AUTH_INVALID),
            (402, ErrorReason.BILLING),
            (403, ErrorReason.AUTH_FORBIDDEN),
        ],
    )
    def test_permanent_status_classifies_non_retryable(self, status, reason) -> None:
        from prodagent.core.error_classifier import classify_error
        from prodagent.core.error_reason import ErrorLayer

        exc = _FakeApiStatusError(f"HTTP {status}", status_code=status)
        classified = classify_error(exc, layer=ErrorLayer.HTTP)
        assert classified.reason is reason
        assert classified.retryable is False

    def test_429_classifies_rate_limited_retryable(self) -> None:
        from prodagent.core.error_classifier import classify_error
        from prodagent.core.error_reason import ErrorLayer

        exc = _FakeApiStatusError("rate limited", status_code=429)
        classified = classify_error(exc, layer=ErrorLayer.HTTP)
        assert classified.reason is ErrorReason.RATE_LIMITED
        assert classified.retryable is True

    def test_503_classifies_overloaded(self) -> None:
        from prodagent.core.error_classifier import classify_error
        from prodagent.core.error_reason import ErrorLayer

        exc = _FakeApiStatusError("service unavailable", status_code=503)
        classified = classify_error(exc, layer=ErrorLayer.HTTP)
        assert classified.reason is ErrorReason.OVERLOADED

    async def test_529_wraps_into_capacity_error(self) -> None:
        async def factory() -> str:
            raise _FakeApiStatusError("capacity", status_code=529)

        with pytest.raises(CapacityError) as excinfo:
            await with_http_retry(factory, policy=RetryPolicy(max_attempts=5))
        cause = excinfo.value.__cause__
        assert isinstance(cause, _FakeApiStatusError)


class TestWithLlmRetryExhaustion:
    async def test_raises_last_exception_after_max_retries(self) -> None:
        call_count = 0

        async def factory() -> str:
            nonlocal call_count
            call_count += 1
            raise _FakeApiStatusError("always 503", status_code=503)

        with pytest.raises(_FakeApiStatusError, match="always 503"):
            await with_http_retry(
                factory,
                policy=RetryPolicy(max_attempts=3, base_delay=0.001, backoff=Backoff.FIXED),
            )
        assert call_count == 3

    async def test_429_retries_then_raises(self) -> None:
        call_count = 0

        async def factory() -> str:
            nonlocal call_count
            call_count += 1
            raise _FakeApiStatusError("rate limited", status_code=429)

        with pytest.raises(_FakeApiStatusError):
            await with_http_retry(
                factory,
                policy=RetryPolicy(max_attempts=3, base_delay=0.001, backoff=Backoff.FIXED),
            )
        assert call_count == 3


class TestWithLlmRetryCapacity:
    async def test_529_raises_capacity_error_immediately(self) -> None:
        call_count = 0

        async def factory() -> str:
            nonlocal call_count
            call_count += 1
            raise _FakeApiStatusError("capacity", status_code=529)

        with pytest.raises(CapacityError):
            await with_http_retry(
                factory,
                policy=RetryPolicy(max_attempts=11, base_delay=0.001, backoff=Backoff.FIXED),
            )
        assert call_count == 1


class TestWithLlmRetryRetryAfterHeader:
    async def test_retry_after_header_returned_for_429(self) -> None:
        call_count = 0
        recorded_sleeps: list[float] = []

        async def factory() -> str:
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise _FakeApiStatusError("rate limited", status_code=429, retry_after="0.001")
            return "ok"

        import prodagent.llm.http_retry as retry_mod

        original_sleep = retry_mod.asyncio.sleep

        async def fake_sleep(seconds: float) -> None:
            recorded_sleeps.append(seconds)

        retry_mod.asyncio.sleep = fake_sleep  # type: ignore[attr-defined]
        try:
            policy = RetryPolicy(max_attempts=3, base_delay=1.0, backoff=Backoff.EXPONENTIAL)
            result = await with_http_retry(factory, policy=policy)
            assert result == "ok"
            assert recorded_sleeps == [0.001]
        finally:
            retry_mod.asyncio.sleep = original_sleep  # type: ignore[attr-defined]


class TestWithLlmRetryNonHttpExceptions:
    async def test_non_whitelisted_exception_is_not_retried(self) -> None:
        call_count = 0

        async def factory() -> str:
            nonlocal call_count
            call_count += 1
            raise RuntimeError("transient network glitch")

        with pytest.raises(RuntimeError, match="transient network glitch"):
            await with_http_retry(
                factory,
                policy=RetryPolicy(max_attempts=5, base_delay=0.001, backoff=Backoff.FIXED),
            )
        assert call_count == 1

    async def test_connection_error_is_retried(self) -> None:
        call_count = 0

        async def factory() -> str:
            nonlocal call_count
            call_count += 1
            if call_count < 3:
                raise ConnectionError("network down")
            return "ok"

        policy = RetryPolicy(max_attempts=5, base_delay=0.001, backoff=Backoff.FIXED)
        result = await with_http_retry(factory, policy=policy)
        assert result == "ok"
        assert call_count == 3

    async def test_non_http_exception_exhaustion(self) -> None:
        call_count = 0

        async def factory() -> str:
            nonlocal call_count
            call_count += 1
            raise ConnectionError("network down")

        with pytest.raises(ConnectionError):
            await with_http_retry(
                factory,
                policy=RetryPolicy(max_attempts=3, base_delay=0.001, backoff=Backoff.FIXED),
            )
        assert call_count == 3

    async def test_cancelled_error_propagates_immediately(self) -> None:
        import asyncio

        call_count = 0

        async def factory() -> str:
            nonlocal call_count
            call_count += 1
            raise asyncio.CancelledError()

        with pytest.raises(asyncio.CancelledError):
            await with_http_retry(
                factory,
                policy=RetryPolicy(max_attempts=5, base_delay=0.001, backoff=Backoff.FIXED),
            )
        assert call_count == 1


class TestWithLlmRetryDefaultPolicy:
    async def test_default_policy_is_jittered(self) -> None:
        call_count = 0

        async def factory() -> str:
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise _FakeApiStatusError("503", status_code=503)
            return "ok"

        import prodagent.llm.http_retry as retry_mod

        original_sleep = retry_mod.asyncio.sleep

        async def fake_sleep(seconds: float) -> None:
            pass

        retry_mod.asyncio.sleep = fake_sleep  # type: ignore[attr-defined]
        try:
            result = await with_http_retry(factory)
            assert result == "ok"
            assert call_count == 2
        finally:
            retry_mod.asyncio.sleep = original_sleep  # type: ignore[attr-defined]
