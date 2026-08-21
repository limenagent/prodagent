"""Transport-level HTTP retry with rate-limit awareness."""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, Any, TypeVar

import httpx

from prodagent.core.error_classifier import (
    PERMANENT_STATUS_CODES,
    RETRYABLE_STATUS_CODES,
)
from prodagent.resilience.reliability.retry import Backoff, RetryPolicy

if TYPE_CHECKING:
    from collections.abc import Callable, Coroutine

logger = logging.getLogger(__name__)

T = TypeVar("T")

RETRYABLE_EXC: tuple[type[BaseException], ...] = (
    asyncio.TimeoutError,
    ConnectionError,
    httpx.TimeoutException,
    httpx.TransportError,
)


class CapacityError(Exception):
    """Raised for 529 (overloaded) responses — capacity pressure, not a fault.
    Retrying immediately makes overload worse; shed load or point the client
    at another endpoint at the caller's routing layer."""


class DeliveryGuard:
    """Marks a streaming response as *partially delivered*.

    Once the consumer has received its first chunk, a mid-stream transport
    failure must not be retried transparently: a fresh attempt would replay
    the already-delivered prefix. The adapter wraps its ``on_chunk`` with
    :meth:`mark` and passes the guard to :func:`with_http_retry`, which then
    lets the failure propagate instead of retrying."""

    __slots__ = ("delivered",)

    def __init__(self) -> None:
        self.delivered: bool = False

    def mark(self) -> None:
        self.delivered = True


def _extract_http_info(exc: Exception) -> tuple[int | None, str | None]:
    """Extract (status_code, retry_after_header) from a provider SDK exception."""
    status: int | None = None
    retry_after: str | None = None

    if hasattr(exc, "status_code"):
        status = exc.status_code

    if hasattr(exc, "response") and exc.response is not None:
        resp = exc.response
        if hasattr(resp, "status_code") and status is None:
            status = resp.status_code
        if hasattr(resp, "headers"):
            retry_after = resp.headers.get("retry-after") or resp.headers.get("Retry-After")

    return status, retry_after


def _delay_for(
    attempt: int,
    retry_after_header: str | None,
    policy: RetryPolicy,
) -> float:
    """Seconds to wait before *attempt* (1-based). Honours Retry-After when present."""
    if retry_after_header:
        try:
            return min(max(float(retry_after_header), 0.0), policy.max_delay)
        except (ValueError, TypeError):
            pass  # HTTP-date form — fall through to policy

    return policy.delay(attempt)


async def with_http_retry(
    coro_factory: Callable[[], Coroutine[Any, Any, T]],
    *,
    policy: RetryPolicy | None = None,
    stream_guard: DeliveryGuard | None = None,
) -> T:
    """Execute an async HTTP call with production-grade retry logic."""
    retry_policy = policy or RetryPolicy(
        max_attempts=6,
        base_delay=1.0,
        max_delay=32.0,
        backoff=Backoff.JITTERED,
    )

    last_exc: Exception | None = None

    for attempt in range(retry_policy.max_attempts):
        try:
            result = await coro_factory()
            if attempt > 0:
                logger.info("HTTP call succeeded on attempt %d", attempt + 1)
            return result

        except asyncio.CancelledError:
            # Never swallow cancellation — caller asked to stop, not retry.
            raise

        except Exception as exc:
            last_exc = exc
            status, retry_after = _extract_http_info(exc)

            if status in PERMANENT_STATUS_CODES:
                logger.error("Permanent HTTP error (HTTP %d) — no retry: %s", status, exc)
                raise

            if status == 529:
                raise CapacityError(
                    "Provider returned 529 (overloaded) — capacity pressure, not a "
                    "fault; shed load or reroute at the caller's routing layer",
                ) from exc

            if stream_guard is not None and stream_guard.delivered:
                # Mid-stream failure after output was already handed to the
                # consumer — a retry would replay delivered chunks.
                logger.error(
                    "Stream failed after delivery — not retrying (would replay "
                    "already-delivered output): %s",
                    exc,
                )
                raise

            is_retryable_status = status in RETRYABLE_STATUS_CODES
            is_retryable_exc = isinstance(exc, RETRYABLE_EXC)
            if not (is_retryable_status or is_retryable_exc):
                logger.error(
                    "Non-retryable exception (%s) — no retry: %s",
                    type(exc).__name__,
                    exc,
                )
                raise

            if attempt >= retry_policy.max_attempts - 1:
                logger.error(
                    "HTTP call failed after %d attempts (last status=%s): %s",
                    retry_policy.max_attempts,
                    status or "unknown",
                    exc,
                )
                raise

            delay = _delay_for(attempt + 1, retry_after, retry_policy)
            logger.warning(
                "HTTP call failed (attempt %d/%d, HTTP %s) — retrying in %.2fs: %s",
                attempt + 1,
                retry_policy.max_attempts,
                status or "?",
                delay,
                exc,
            )
            await asyncio.sleep(delay)

    raise last_exc  # type: ignore[misc]
