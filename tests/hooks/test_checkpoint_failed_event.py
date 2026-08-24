from __future__ import annotations

import pytest

from prodagent.hooks import fire_checkpoint_failed
from prodagent.kernel.bus import HookEvent, HookRegistry
from prodagent.kernel.state import AgentRun


def _run() -> AgentRun:
    return AgentRun(run_id="R1", task="t")


@pytest.mark.asyncio
async def test_fires_on_false_to_true_edge():
    hooks = HookRegistry()
    seen: list[str] = []
    hooks.register_event(HookEvent.CHECKPOINT_FAILED, lambda **kw: seen.append(kw["run_id"]))

    run = _run()
    run.checkpoint_failed = True
    await fire_checkpoint_failed(hooks, run, was_failed=False)

    assert seen == ["R1"]


@pytest.mark.asyncio
async def test_does_not_refire_when_already_failed():
    hooks = HookRegistry()
    seen: list[str] = []
    hooks.register_event(HookEvent.CHECKPOINT_FAILED, lambda **kw: seen.append(kw["run_id"]))

    run = _run()
    run.checkpoint_failed = True
    await fire_checkpoint_failed(hooks, run, was_failed=True)

    assert seen == []


@pytest.mark.asyncio
async def test_no_fire_when_save_succeeds():
    hooks = HookRegistry()
    seen: list[str] = []
    hooks.register_event(HookEvent.CHECKPOINT_FAILED, lambda **kw: seen.append(kw["run_id"]))

    run = _run()
    await fire_checkpoint_failed(hooks, run, was_failed=False)

    assert seen == []


@pytest.mark.asyncio
async def test_noop_without_hooks():
    run = _run()
    run.checkpoint_failed = True
    await fire_checkpoint_failed(None, run, was_failed=False)
