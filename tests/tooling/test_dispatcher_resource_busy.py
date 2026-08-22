"""RESOURCE_BUSY semantics — contention is the LLM's call, not the executor's.

Chapter 10 ("isolation over sharing"): the executor no longer serialises
``resource_id`` writes (locks are the tool's own concern), and a tool that
reports ``resource_busy`` must reach the LLM untouched — message + hint, no
mechanical executor retry, so the agent can yield to another task.
"""

from __future__ import annotations

import asyncio

import pytest

from prodagent import SideEffectLevel, ToolMeta
from prodagent.core.error_reason import ErrorReason
from prodagent.core.state import AgentRun
from prodagent.core.types import ErrorSeverity, ToolCall, ToolOutcome
from prodagent.tooling import tool
from prodagent.tooling.dispatcher import ToolDispatcher


def _busy_dict(message: str) -> dict:
    return {
        "error": True,
        "reason": "resource_busy",
        "code": "resource_busy",
        "error_severity": "yellow",
        "message": message,
        "hint": "Try an alternative task or retry later.",
    }


@pytest.mark.asyncio
async def test_same_resource_id_calls_run_concurrently():
    """The dispatcher is lock-agnostic: two non-readonly tools sharing a
    resource_id run in parallel — serialisation is the tool's own job now."""
    concurrent = 0
    max_concurrent = 0

    def _make(name: str):
        @tool(
            name=name,
            meta=ToolMeta(
                name=name,
                side_effect_level=SideEffectLevel.MEDIUM,
                timeout_seconds=2.0,
                resource_id="shared-resource",
            ),
        )
        async def fn() -> dict:
            nonlocal concurrent, max_concurrent
            concurrent += 1
            max_concurrent = max(max_concurrent, concurrent)
            await asyncio.sleep(0.05)
            concurrent -= 1
            return {"ok": True}

        return fn

    write_a = _make("write_a")
    write_b = _make("write_b")
    dispatcher = ToolDispatcher({write_a.name: write_a, write_b.name: write_b})

    results = await asyncio.gather(
        dispatcher.dispatch(ToolCall(name="write_a", params={})),
        dispatcher.dispatch(ToolCall(name="write_b", params={})),
    )

    assert max_concurrent == 2, "same resource_id must NOT be serialised by the dispatcher"
    assert all(r.outcome is ToolOutcome.OK for r in results)


@pytest.mark.asyncio
async def test_resource_busy_not_retried_by_dispatcher(monkeypatch):
    sleeps: list[float] = []

    async def _fake_sleep(d: float) -> None:
        sleeps.append(d)

    monkeypatch.setattr("prodagent.tooling.dispatcher.asyncio.sleep", _fake_sleep)

    attempts = 0

    @tool(
        name="busy_tool",
        meta=ToolMeta(
            name="busy_tool",
            side_effect_level=SideEffectLevel.MEDIUM,
        ),
    )
    async def busy_tool() -> dict:
        nonlocal attempts
        attempts += 1
        return _busy_dict(f"Resource 'orders' is busy (attempt {attempts}).")

    dispatcher = ToolDispatcher({busy_tool.name: busy_tool})
    run = AgentRun(run_id="r-busy", task="t")
    result = await dispatcher.dispatch_with_retry(ToolCall(name="busy_tool", params={}), run)

    assert attempts == 1, "RESOURCE_BUSY must be deferred to the LLM, not retried"
    assert sleeps == []
    assert run.retry_count("busy_tool") == 0
    assert result.error is not None
    assert result.error.reason is ErrorReason.RESOURCE_BUSY
    assert result.error.error_severity is ErrorSeverity.YELLOW
    assert result.outcome is ToolOutcome.RETRY
    assert result.error.hint == "Try an alternative task or retry later."


@pytest.mark.asyncio
async def test_resource_busy_returns_first_attempt_even_with_retry_budget():
    """A retry budget must not burn on contention — the LLM sees attempt #1's error."""
    attempts = 0

    @tool(
        name="busy_tool2",
        meta=ToolMeta(
            name="busy_tool2",
            side_effect_level=SideEffectLevel.MEDIUM,
        ),
    )
    async def busy_tool2() -> dict:
        nonlocal attempts
        attempts += 1
        return _busy_dict(f"busy-{attempts}")

    dispatcher = ToolDispatcher({busy_tool2.name: busy_tool2})
    run = AgentRun(run_id="r-busy2", task="t")
    result = await dispatcher.dispatch_with_retry(ToolCall(name="busy_tool2", params={}), run)

    assert attempts == 1
    assert result.error is not None
    assert result.error.message == "busy-1"


@pytest.mark.asyncio
async def test_yellow_non_busy_reason_still_retries(monkeypatch):
    """The RESOURCE_BUSY guard must not swallow ordinary YELLOW retries."""
    sleeps: list[float] = []

    async def _fake_sleep(d: float) -> None:
        sleeps.append(d)

    monkeypatch.setattr("prodagent.tooling.dispatcher.asyncio.sleep", _fake_sleep)

    attempts = 0

    @tool(
        name="flaky_conn",
        meta=ToolMeta(
            name="flaky_conn",
            side_effect_level=SideEffectLevel.MEDIUM,
        ),
    )
    async def flaky_conn() -> dict:
        nonlocal attempts
        attempts += 1
        return {
            "error": True,
            "reason": "connection",
            "message": f"conn refused (attempt {attempts})",
            "hint": "retry with backoff",
        }

    from prodagent.tooling.retry import Backoff, RetryPolicy

    dispatcher = ToolDispatcher(
        {flaky_conn.name: flaky_conn},
        retry_policy=RetryPolicy(max_attempts=4, base_delay=0.0, backoff=Backoff.FIXED),
    )
    run = AgentRun(run_id="r-conn", task="t")
    await dispatcher.dispatch_with_retry(ToolCall(name="flaky_conn", params={}), run)

    assert attempts == 4, "CONNECTION-reason YELLOW must still retry 1+3 times"
