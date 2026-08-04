from __future__ import annotations

from prodagent import ToolMeta
from prodagent.tooling import tool
from prodagent.tooling.registry import ToolRegistry
from prodagent.tooling.reliability.circuit_breaker import ToolCircuitBreaker


def _make_tool(name: str, *, readonly: bool = True, domain: str = "general"):
    @tool(
        name=name, readonly=readonly, meta=ToolMeta(name=name, domain=domain, is_readonly=readonly)
    )
    def fn() -> dict:
        return {}

    return fn


async def test_l1_tools_always_visible():
    reg = ToolRegistry()
    t = _make_tool("check_slo", readonly=True)
    reg.register(t, tier="l1")

    active = await reg.get_active_tools(role="guest")
    assert any(x.name == "check_slo" for x in active)


async def test_l2_tools_filtered_by_role():
    reg = ToolRegistry()
    t = _make_tool("restart_pod", readonly=False)
    reg.register(t, tier="l2", role="oncall")

    active_oncall = await reg.get_active_tools(role="oncall")
    active_guest = await reg.get_active_tools(role="guest")

    assert any(x.name == "restart_pod" for x in active_oncall)
    assert not any(x.name == "restart_pod" for x in active_guest)


async def test_l3_tools_excluded_without_query():
    reg = ToolRegistry()
    t = _make_tool("legacy_api", readonly=True)
    reg.register(t, tier="l3")

    active = await reg.get_active_tools(role="general")
    active_names = [x.name for x in active]
    assert "legacy_api" not in active_names


async def test_l3_tools_included_with_matching_query():
    reg = ToolRegistry()
    t = _make_tool("legacy_billing", readonly=True)
    reg.register(t, tier="l3")

    active = await reg.get_active_tools(role="general", force_l3_query="legacy")
    names = [x.name for x in active]
    assert "legacy_billing" in names


def test_invalid_tier_raises():
    reg = ToolRegistry()
    t = _make_tool("bad_tool")
    import pytest

    with pytest.raises(ValueError, match="Unknown tier"):
        reg.register(t, tier="l4")


async def test_schemas_for_returns_list_of_dicts():
    reg = ToolRegistry()
    t = _make_tool("query_metrics", readonly=True)
    reg.register(t, tier="l1")
    active = await reg.get_active_tools()
    schemas = reg.schemas_for(active)
    assert isinstance(schemas, list)
    assert all(isinstance(s, dict) for s in schemas)
    assert any(s["name"] == "query_metrics" for s in schemas)


def test_get_meta_returns_tool_meta():
    reg = ToolRegistry()
    t = _make_tool("tail_logs", readonly=True)
    reg.register(t, tier="l1")
    meta = reg.get_meta("tail_logs")
    assert meta.name == "tail_logs"
    assert meta.is_readonly is True


async def test_circuit_starts_closed():
    cb = ToolCircuitBreaker(failure_threshold=3)
    assert await cb.is_available("my_tool")
    assert (await cb.status("my_tool"))["state"] == "closed"


async def test_circuit_opens_after_threshold():
    cb = ToolCircuitBreaker(failure_threshold=3)
    for _ in range(3):
        await cb.record_failure("flaky_tool")
    assert not await cb.is_available("flaky_tool")
    assert (await cb.status("flaky_tool"))["state"] == "open"


async def test_circuit_closes_after_success():
    cb = ToolCircuitBreaker(failure_threshold=2, recovery_timeout_seconds=0.05)
    await cb.record_failure("t")
    await cb.record_failure("t")
    assert not await cb.is_available("t")
    import time

    time.sleep(0.06)
    # is_available re-offers the tool after the recovery window but never flips
    # state — the probe claims the OPEN -> HALF_OPEN transition.
    assert await cb.is_available("t")
    assert (await cb.status("t"))["state"] == "open"
    assert await cb.try_acquire_probe("t")
    assert (await cb.status("t"))["state"] == "half_open"
    await cb.record_success("t")
    assert await cb.is_available("t")
    assert (await cb.status("t"))["state"] == "closed"


async def test_half_open_probe_failure_reopens():
    cb = ToolCircuitBreaker(failure_threshold=2, recovery_timeout_seconds=0.05)
    await cb.record_failure("t")
    await cb.record_failure("t")
    import time

    time.sleep(0.06)
    assert await cb.is_available("t")
    assert await cb.try_acquire_probe("t")
    assert (await cb.status("t"))["state"] == "half_open"
    await cb.record_failure("t")
    assert (await cb.status("t"))["state"] == "open"


async def test_registry_excludes_open_circuit_tools():
    reg = ToolRegistry(failure_threshold=2)
    t = _make_tool("broken_tool", readonly=True)
    reg.register(t, tier="l1")

    await reg.record_failure("broken_tool")
    await reg.record_failure("broken_tool")

    active = await reg.get_active_tools()
    assert not any(x.name == "broken_tool" for x in active)
