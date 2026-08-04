from __future__ import annotations

import time

from prodagent.tooling.reliability.circuit_breaker import ToolCircuitBreaker


async def test_failure_count_starts_at_zero():
    cb = ToolCircuitBreaker(failure_threshold=3, window_seconds=60.0)
    assert (await cb.status("t"))["failures"] == 0
    assert (await cb.status("t"))["state"] == "closed"


async def test_failures_within_window_counted():
    cb = ToolCircuitBreaker(failure_threshold=10, window_seconds=60.0)
    await cb.record_failure("t")
    await cb.record_failure("t")
    await cb.record_failure("t")
    assert (await cb.status("t"))["failures"] == 3


async def test_expired_failures_evicted_on_status():
    cb = ToolCircuitBreaker(failure_threshold=10, window_seconds=0.05)
    await cb.record_failure("t")
    await cb.record_failure("t")
    assert (await cb.status("t"))["failures"] == 2

    time.sleep(0.06)
    assert (await cb.status("t"))["failures"] == 0


async def test_expired_failures_evicted_on_record_failure():
    cb = ToolCircuitBreaker(failure_threshold=10, window_seconds=0.05)
    await cb.record_failure("t")
    assert (await cb.status("t"))["failures"] == 1

    time.sleep(0.06)
    await cb.record_failure("t")
    assert (await cb.status("t"))["failures"] == 1


async def test_threshold_checked_against_windowed_count():
    cb = ToolCircuitBreaker(failure_threshold=2, window_seconds=0.05)
    await cb.record_failure("t")
    time.sleep(0.06)
    await cb.record_failure("t")
    assert (await cb.status("t"))["state"] == "closed"
    await cb.record_failure("t")
    assert (await cb.status("t"))["state"] == "open"


async def test_record_success_clears_window():
    cb = ToolCircuitBreaker(failure_threshold=5, window_seconds=60.0)
    await cb.record_failure("t")
    await cb.record_failure("t")
    await cb.record_success("t")
    assert (await cb.status("t"))["failures"] == 0
    assert (await cb.status("t"))["state"] == "closed"


async def test_open_transitions_to_half_open_after_recovery_window():
    cb = ToolCircuitBreaker(failure_threshold=2, recovery_timeout_seconds=0.05)
    await cb.record_failure("t")
    await cb.record_failure("t")
    assert (await cb.status("t"))["state"] == "open"

    time.sleep(0.06)
    # is_available re-offers the tool (pure query) without transitioning;
    # try_acquire_probe claims the OPEN -> HALF_OPEN transition.
    assert await cb.is_available("t") is True
    assert (await cb.status("t"))["state"] == "open"
    assert await cb.try_acquire_probe("t") is True
    assert (await cb.status("t"))["state"] == "half_open"


async def test_half_open_probe_success_closes():
    cb = ToolCircuitBreaker(failure_threshold=2, recovery_timeout_seconds=0.05)
    await cb.record_failure("t")
    await cb.record_failure("t")
    time.sleep(0.06)
    await cb.is_available("t")
    await cb.record_success("t")
    assert (await cb.status("t"))["state"] == "closed"


async def test_half_open_probe_failure_reopens():
    cb = ToolCircuitBreaker(failure_threshold=2, recovery_timeout_seconds=0.05)
    await cb.record_failure("t")
    await cb.record_failure("t")
    time.sleep(0.06)
    await cb.is_available("t")
    await cb.record_failure("t")
    assert (await cb.status("t"))["state"] == "open"


async def test_try_acquire_probe_closed_always_true():
    cb = ToolCircuitBreaker(failure_threshold=3)
    assert await cb.try_acquire_probe("t") is True


async def test_try_acquire_probe_half_open_single_probe():
    cb = ToolCircuitBreaker(failure_threshold=1, recovery_timeout_seconds=0.05)
    await cb.record_failure("t")
    time.sleep(0.06)
    assert await cb.try_acquire_probe("t") is True
    assert await cb.try_acquire_probe("t") is False


async def test_release_probe_frees_slot():
    cb = ToolCircuitBreaker(failure_threshold=1, recovery_timeout_seconds=0.05)
    await cb.record_failure("t")
    time.sleep(0.06)
    assert await cb.try_acquire_probe("t") is True
    await cb.release_probe("t")
    assert await cb.try_acquire_probe("t") is True


async def test_window_size_caps_deque():
    cb = ToolCircuitBreaker(failure_threshold=100, window_seconds=60.0, window_size=5)
    for _ in range(10):
        await cb.record_failure("t")
    assert (await cb.status("t"))["failures"] == 5


async def test_default_window_is_300_seconds():
    cb = ToolCircuitBreaker()
    assert cb._window == 300.0
