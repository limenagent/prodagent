from __future__ import annotations

import asyncio

import pytest

from prodagent.kernel.bus import HookEvent, HookRegistry, InjectionPoint


@pytest.mark.asyncio
async def test_injection_failure_awaits_async_handler_before_collect_returns():
    hooks = HookRegistry()
    seen: dict[str, str] = {}

    async def audit_handler(*, event_name: str, point: str, injector: str, error: str) -> None:
        await asyncio.sleep(0)
        seen["point"] = point
        seen["injector"] = injector
        seen["error"] = error

    hooks.register_event(HookEvent.INJECTION_FAILED, audit_handler)

    def failing_injector(**_: object) -> None:
        raise RuntimeError("recall backend down")

    hooks.register_injector(InjectionPoint.CONTEXT_INJECTOR, failing_injector)

    results = await hooks.collect(InjectionPoint.CONTEXT_INJECTOR, query="x")

    assert results == []
    assert seen == {
        "point": InjectionPoint.CONTEXT_INJECTOR.value,
        "injector": "test_injection_failure_awaits_async_handler_before_collect_returns.<locals>.failing_injector",
        "error": "recall backend down",
    }, "async INJECTION_FAILED handler must run to completion before collect returns"


@pytest.mark.asyncio
async def test_injection_failure_sync_handler_still_runs():
    hooks = HookRegistry()
    seen: list[str] = []

    def sync_handler(*, event_name: str, point: str, **_: object) -> None:
        seen.append(point)

    hooks.register_event(HookEvent.INJECTION_FAILED, sync_handler)

    def failing_injector(**_: object) -> None:
        raise ValueError("boom")

    hooks.register_injector(InjectionPoint.CONTEXT_INJECTOR, failing_injector)

    await hooks.collect(InjectionPoint.CONTEXT_INJECTOR, query="q")

    assert seen == [InjectionPoint.CONTEXT_INJECTOR.value]


@pytest.mark.asyncio
async def test_injection_failure_handler_exception_does_not_break_collect():
    hooks = HookRegistry()

    async def broken_audit_handler(**_: object) -> None:
        raise RuntimeError("audit backend also down")

    hooks.register_event(HookEvent.INJECTION_FAILED, broken_audit_handler)

    def failing_injector(**_: object) -> None:
        raise RuntimeError("primary failure")

    hooks.register_injector(InjectionPoint.CONTEXT_INJECTOR, failing_injector)

    results = await hooks.collect(InjectionPoint.CONTEXT_INJECTOR, query="q")
    assert results == []


@pytest.mark.asyncio
async def test_injection_failure_does_not_recurse_via_fire():
    hooks = HookRegistry()
    fire_call_count = 0
    original_fire = hooks.fire

    async def counting_fire(event: HookEvent, **data: object) -> None:
        nonlocal fire_call_count
        fire_call_count += 1
        await original_fire(event, **data)

    hooks.fire = counting_fire  # type: ignore[method-assign]

    try:

        def failing_injector(**_: object) -> None:
            raise RuntimeError("down")

        def failing_injection_failed_handler(**_: object) -> None:
            raise RuntimeError("handler also broken")

        hooks.register_injector(InjectionPoint.CONTEXT_INJECTOR, failing_injector)
        hooks.register_event(HookEvent.INJECTION_FAILED, failing_injection_failed_handler)
        await hooks.collect(InjectionPoint.CONTEXT_INJECTOR, query="q")
    finally:
        hooks.fire = original_fire  # type: ignore[method-assign]

    assert fire_call_count == 0, "INJECTION_FAILED must bypass self.fire to avoid recursion"
