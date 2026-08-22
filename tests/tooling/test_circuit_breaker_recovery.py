from __future__ import annotations

import time

from prodagent.core.types import ToolMeta
from prodagent.tooling import tool
from prodagent.tooling.registry import ToolRegistry
from prodagent.tooling.reliability.circuit_breaker import (
    ToolCircuitBreaker,
)


def _make_tool(name: str):
    @tool(name=name, meta=ToolMeta(name=name, is_readonly=True))
    async def _fn() -> dict:
        return {}

    return _fn


async def test_half_open_tool_stays_in_active_tools():
    reg = ToolRegistry(failure_threshold=2, recovery_timeout_seconds=0.05)
    t = _make_tool("flaky")
    reg.register(t, tier="l1")

    await reg.record_failure("flaky")
    await reg.record_failure("flaky")
    assert (await reg._breaker.status("flaky"))["state"] == "open"

    time.sleep(0.06)
    active = await reg.get_active_tools()
    names = [x.name for x in active]
    assert "flaky" in names, "recovered tool was not re-offered after the recovery window"
    # Listing must not flip the breaker — the probe claims OPEN -> HALF_OPEN.
    assert (await reg._breaker.status("flaky"))["state"] == "open"
    assert await reg.try_acquire_probe("flaky") is True
    assert (await reg._breaker.status("flaky"))["state"] == "half_open"


async def test_open_within_recovery_window_filtered_out():
    reg = ToolRegistry(failure_threshold=1, recovery_timeout_seconds=60.0)
    t = _make_tool("dead")
    reg.register(t, tier="l1")

    await reg.record_failure("dead")
    assert (await reg._breaker.status("dead"))["state"] == "open"

    active = await reg.get_active_tools()
    assert "dead" not in [x.name for x in active]


async def test_try_acquire_probe_closed_returns_true():
    cb = ToolCircuitBreaker(failure_threshold=3)
    assert await cb.try_acquire_probe("t") is True


async def test_try_acquire_probe_open_within_window_returns_false():
    cb = ToolCircuitBreaker(failure_threshold=1, recovery_timeout_seconds=60.0)
    await cb.record_failure("t")
    assert (await cb.status("t"))["state"] == "open"
    assert await cb.try_acquire_probe("t") is False


async def test_try_acquire_probe_half_open_admits_one_probe():
    cb = ToolCircuitBreaker(failure_threshold=1, recovery_timeout_seconds=0.05)
    await cb.record_failure("t")
    assert (await cb.status("t"))["state"] == "open"

    time.sleep(0.06)
    assert await cb.try_acquire_probe("t") is True
    assert (await cb.status("t"))["state"] == "half_open"
    assert await cb.try_acquire_probe("t") is False
    assert await cb.try_acquire_probe("t") is False


async def test_probe_released_on_success_allows_next_probe():
    cb = ToolCircuitBreaker(failure_threshold=1, recovery_timeout_seconds=0.05)
    await cb.record_failure("t")
    time.sleep(0.06)

    assert await cb.try_acquire_probe("t") is True
    await cb.record_success("t")
    assert (await cb.status("t"))["state"] == "closed"
    assert await cb.try_acquire_probe("t") is True


async def test_probe_released_on_failure_reopens_breaker():
    cb = ToolCircuitBreaker(failure_threshold=1, recovery_timeout_seconds=0.05)
    await cb.record_failure("t")
    time.sleep(0.06)

    assert await cb.try_acquire_probe("t") is True
    await cb.record_failure("t")
    assert (await cb.status("t"))["state"] == "open"


async def test_release_probe_frees_slot_without_state_change():
    cb = ToolCircuitBreaker(failure_threshold=1, recovery_timeout_seconds=0.05)
    await cb.record_failure("t")
    time.sleep(0.06)

    assert await cb.try_acquire_probe("t") is True
    assert await cb.try_acquire_probe("t") is False
    await cb.release_probe("t")
    assert await cb.try_acquire_probe("t") is True
    assert (await cb.status("t"))["state"] == "half_open"


async def test_registry_recovery_flow_open_half_open_closed():
    reg = ToolRegistry(failure_threshold=2, recovery_timeout_seconds=0.05)

    await reg.record_failure("t")
    await reg.record_failure("t")
    assert (await reg._breaker.status("t"))["state"] == "open"

    time.sleep(0.06)
    assert await reg.is_available("t") is True
    assert await reg.try_acquire_probe("t") is True
    await reg.record_success("t")
    assert (await reg._breaker.status("t"))["state"] == "closed"
    assert await reg.try_acquire_probe("t") is True
