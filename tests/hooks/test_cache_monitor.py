from __future__ import annotations

import logging

import pytest

from prodagent.hooks.observers.cache_monitor import CacheMonitorHooks
from prodagent.kernel.bus import HookEvent, HookRegistry

pytestmark = pytest.mark.asyncio


async def test_warmup_turns_are_ignored(caplog: pytest.LogCaptureFixture) -> None:
    hooks = HookRegistry()
    CacheMonitorHooks(threshold=0.3, warmup_turns=2).attach(hooks)

    with caplog.at_level(logging.WARNING, logger="prodagent.hooks.observers.cache_monitor"):
        await hooks.fire(HookEvent.TOKEN_UPDATE, turn=1, cache_hit_ratio=0.0, run_id="r1")
        await hooks.fire(HookEvent.TOKEN_UPDATE, turn=2, cache_hit_ratio=0.0, run_id="r1")

    assert not caplog.records


async def test_warns_once_when_ratio_below_threshold_after_warmup(
    caplog: pytest.LogCaptureFixture,
) -> None:
    hooks = HookRegistry()
    CacheMonitorHooks(threshold=0.3, warmup_turns=2).attach(hooks)

    with caplog.at_level(logging.WARNING, logger="prodagent.hooks.observers.cache_monitor"):
        await hooks.fire(HookEvent.TOKEN_UPDATE, turn=3, cache_hit_ratio=0.1, run_id="r1")
        await hooks.fire(HookEvent.TOKEN_UPDATE, turn=4, cache_hit_ratio=0.1, run_id="r1")

    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1
    assert "r1" in warnings[0].message


async def test_no_warning_when_ratio_meets_threshold(
    caplog: pytest.LogCaptureFixture,
) -> None:
    hooks = HookRegistry()
    CacheMonitorHooks(threshold=0.3, warmup_turns=2).attach(hooks)

    with caplog.at_level(logging.WARNING, logger="prodagent.hooks.observers.cache_monitor"):
        await hooks.fire(HookEvent.TOKEN_UPDATE, turn=5, cache_hit_ratio=0.5, run_id="r2")

    assert not caplog.records


async def test_different_runs_each_get_their_own_warning(
    caplog: pytest.LogCaptureFixture,
) -> None:
    hooks = HookRegistry()
    CacheMonitorHooks(threshold=0.3, warmup_turns=2).attach(hooks)

    with caplog.at_level(logging.WARNING, logger="prodagent.hooks.observers.cache_monitor"):
        await hooks.fire(HookEvent.TOKEN_UPDATE, turn=3, cache_hit_ratio=0.1, run_id="r1")
        await hooks.fire(HookEvent.TOKEN_UPDATE, turn=3, cache_hit_ratio=0.1, run_id="r2")

    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 2
