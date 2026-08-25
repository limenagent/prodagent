from __future__ import annotations

import asyncio
import time

from prodagent import Agent, AgentConfig
from prodagent.hooks.observers.console import ConsoleObserverHooks
from prodagent.kernel.bus import Gate, HookEvent, HookRegistry, InjectionPoint


def _has_console_observer(registry: HookRegistry) -> bool:
    all_handlers: list = []
    for event in HookEvent:
        all_handlers.extend(registry.event_handlers(event))
    return any(
        getattr(h, "__self__", None).__class__ is ConsoleObserverHooks
        or "ConsoleObserver" in getattr(h, "__qualname__", "")
        for h in all_handlers
    )


def test_extend_preserves_default_observers(monkeypatch):
    """User extensions compose with — not replace — the default bundles.

    Console output is opt-in since the lightweight pass (PRODAGENT_CONSOLE=1):
    a library stays silent on stdout by default, and enabling it must not be
    wiped out by a user extension bundle either.
    """

    class NoopBundle:
        def attach(self, hooks: HookRegistry) -> None:
            hooks.register_checker(Gate.TOOL_CALL, lambda **_: None)

    agent = Agent("t", config=AgentConfig(name="t", extensions=[NoopBundle()]))

    resolved = agent.attach_default_hooks()
    assert not _has_console_observer(resolved)  # opt-in: silent by default

    monkeypatch.setenv("PRODAGENT_CONSOLE", "1")
    enabled = Agent("t", config=AgentConfig(name="t", extensions=[NoopBundle()]))

    resolved_enabled = enabled.attach_default_hooks()
    assert _has_console_observer(resolved_enabled)  # user bundle didn't wipe it


async def test_injectors_run_concurrently_via_gather():
    hooks = HookRegistry()

    async def slow_injector_1(**_):
        await asyncio.sleep(0.05)
        return "injector-1"

    async def slow_injector_2(**_):
        await asyncio.sleep(0.05)
        return "injector-2"

    async def slow_injector_3(**_):
        await asyncio.sleep(0.05)
        return "injector-3"

    hooks.register_injector(InjectionPoint.CONTEXT_INJECTOR, slow_injector_1)
    hooks.register_injector(InjectionPoint.CONTEXT_INJECTOR, slow_injector_2)
    hooks.register_injector(InjectionPoint.CONTEXT_INJECTOR, slow_injector_3)

    start = time.monotonic()
    results = await hooks.collect(InjectionPoint.CONTEXT_INJECTOR)
    elapsed = time.monotonic() - start

    assert set(results) == {"injector-1", "injector-2", "injector-3"}
    assert elapsed < 0.12, (
        f"Injectors should run concurrently (~50ms), but elapsed {elapsed * 1000:.0f}ms "
        "(serial would be ~150ms) — gather not applied?"
    )


async def test_event_observers_run_concurrently():
    hooks = HookRegistry()

    observed_times: list[float] = []

    async def slow_observer_1(**_):
        await asyncio.sleep(0.05)
        observed_times.append(time.monotonic())

    async def slow_observer_2(**_):
        await asyncio.sleep(0.05)
        observed_times.append(time.monotonic())

    hooks.register_event(HookEvent.TOKEN_UPDATE, slow_observer_1)
    hooks.register_event(HookEvent.TOKEN_UPDATE, slow_observer_2)

    start = time.monotonic()
    await hooks.fire(HookEvent.TOKEN_UPDATE, turn=1)
    elapsed = time.monotonic() - start

    assert len(observed_times) == 2
    assert elapsed < 0.09, (
        f"Event observers should run concurrently (~50ms), but elapsed {elapsed * 1000:.0f}ms"
    )


async def test_veto_on_first_stays_serial():
    hooks = HookRegistry()

    ran_second: list[bool] = []

    async def first_checker_vetoes(**_):
        await asyncio.sleep(0.05)
        from prodagent.kernel.bus import BlockingResult

        return BlockingResult(blocked=True, reason="vetoed by first checker")

    async def second_checker_should_not_run(**_):
        ran_second.append(True)

    hooks.register_checker(Gate.TOOL_CALL, first_checker_vetoes, priority=100)
    hooks.register_checker(Gate.TOOL_CALL, second_checker_should_not_run, priority=90)

    result = await hooks.check_blocking(Gate.TOOL_CALL, name="t")
    assert result.blocked
    assert result.reason == "vetoed by first checker"
    assert not ran_second, "second checker must NOT run after a veto (serial short-circuit)"


async def test_collector_results_preserve_priority_order_for_sync_handlers():
    hooks = HookRegistry()

    def sync_handler(**_):
        return "sync-result"

    async def async_handler(**_):
        await asyncio.sleep(0.01)
        return "async-result"

    hooks.register_injector(InjectionPoint.CONTEXT_INJECTOR, sync_handler, priority=100)
    hooks.register_injector(InjectionPoint.CONTEXT_INJECTOR, async_handler, priority=50)

    results = await hooks.collect(InjectionPoint.CONTEXT_INJECTOR)
    assert results == ["sync-result", "async-result"], (
        "sync handler result must land before async handler result (priority order)"
    )


async def test_injector_failure_does_not_break_concurrent_batch():
    hooks = HookRegistry()

    async def good_injector_1(**_):
        await asyncio.sleep(0.02)
        return "good-1"

    async def bad_injector(**_):
        await asyncio.sleep(0.01)
        raise RuntimeError("injector failed")

    async def good_injector_2(**_):
        await asyncio.sleep(0.02)
        return "good-2"

    hooks.register_injector(InjectionPoint.CONTEXT_INJECTOR, good_injector_1, priority=100)
    hooks.register_injector(InjectionPoint.CONTEXT_INJECTOR, bad_injector, priority=50)
    hooks.register_injector(InjectionPoint.CONTEXT_INJECTOR, good_injector_2, priority=10)

    results = await hooks.collect(InjectionPoint.CONTEXT_INJECTOR)
    assert "good-1" in results
    assert "good-2" in results
    assert len(results) == 2
