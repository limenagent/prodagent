from __future__ import annotations

import pytest

from prodagent import SideEffectLevel, ToolMeta
from prodagent.base.errors import ErrorReason
from prodagent.base.retry import Backoff, RetryPolicy
from prodagent.kernel.run import Run
from prodagent.kernel.types import ErrorSeverity, ToolCall, ToolError, ToolOutcome
from prodagent.tooling import tool
from prodagent.tooling.dispatcher import ToolDispatcher, _default_tool_retry_policy


def _yellow_result(message: str = "transient") -> ToolError:
    return ToolError.from_reason(
        ErrorReason.CONNECTION,
        code="transient_failure",
        message=message,
        hint="retry after backoff",
    )


def _red_result(message: str = "permanent") -> ToolError:
    return ToolError.from_reason(
        ErrorReason.FORMAT_ERROR,
        code="permanent_failure",
        message=message,
        hint="do not retry",
    )


def test_default_tool_retry_policy_is_one_attempt():
    """Retries are opt-in: silently re-running a side-effecting tool is a
    surprise, not resilience. Callers pass retry_policy= explicitly."""
    p = _default_tool_retry_policy()
    assert p.max_attempts == 1
    assert p.backoff is Backoff.FIXED


@pytest.mark.asyncio
async def test_dispatch_with_retry_default_does_not_retry(monkeypatch):
    """Default policy: one attempt, no sleeps — a YELLOW error surfaces
    immediately instead of being silently retried."""
    sleeps: list[float] = []

    async def _fake_sleep(d: float) -> None:
        sleeps.append(d)

    monkeypatch.setattr("prodagent.tooling.dispatcher.asyncio.sleep", _fake_sleep)

    attempts = 0

    @tool(
        name="flaky",
        meta=ToolMeta(name="flaky", side_effect_level=SideEffectLevel.MEDIUM),
    )
    async def flaky() -> ToolError:
        nonlocal attempts
        attempts += 1
        return _yellow_result(f"try-{attempts}")

    run = Run(run_id="r1", task="t")
    call = ToolCall(name="flaky", params={})
    dispatcher = ToolDispatcher({flaky.name: flaky})

    result = await dispatcher.dispatch_with_retry(call, run)

    assert attempts == 1
    assert sleeps == []
    assert run.retry_count("flaky") == 0
    assert result.error is not None
    assert result.error.error_severity is ErrorSeverity.YELLOW


@pytest.mark.asyncio
async def test_dispatch_with_retry_honours_custom_policy(monkeypatch):
    sleeps: list[float] = []

    async def _fake_sleep(d: float) -> None:
        sleeps.append(d)

    monkeypatch.setattr("prodagent.tooling.dispatcher.asyncio.sleep", _fake_sleep)

    attempts = 0

    @tool(
        name="flaky2",
        meta=ToolMeta(name="flaky2", side_effect_level=SideEffectLevel.MEDIUM),
    )
    async def flaky2() -> ToolError:
        nonlocal attempts
        attempts += 1
        return _yellow_result()

    policy = RetryPolicy(
        max_attempts=2,
        base_delay=0.5,
        max_delay=1.0,
        backoff=Backoff.EXPONENTIAL,
    )
    dispatcher = ToolDispatcher({flaky2.name: flaky2}, retry_policy=policy)
    run = Run(run_id="r2", task="t")
    call = ToolCall(name="flaky2", params={})

    await dispatcher.dispatch_with_retry(call, run)

    assert attempts == 2, f"custom policy should cap at 2 attempts, got {attempts}"
    assert sleeps == [0.5], f"expected one 0.5s sleep, got {sleeps}"
    assert run.retry_count("flaky2") == 1


@pytest.mark.asyncio
async def test_dispatch_with_retry_red_does_not_retry():
    attempts = 0

    @tool(
        name="bad",
        meta=ToolMeta(name="bad", side_effect_level=SideEffectLevel.MEDIUM),
    )
    async def bad() -> ToolError:
        nonlocal attempts
        attempts += 1
        return _red_result()

    dispatcher = ToolDispatcher({bad.name: bad})
    run = Run(run_id="r3", task="t")
    call = ToolCall(name="bad", params={})

    result = await dispatcher.dispatch_with_retry(call, run)

    assert attempts == 1, "RED must not retry"
    assert result.error is not None
    assert result.error.error_severity is ErrorSeverity.RED
    assert run.retry_count("bad") == 0


@pytest.mark.asyncio
async def test_dispatch_with_retry_ok_returns_immediately():
    attempts = 0

    @tool(
        name="ok_tool",
        meta=ToolMeta(name="ok_tool", side_effect_level=SideEffectLevel.MEDIUM),
    )
    async def ok_tool() -> dict:
        nonlocal attempts
        attempts += 1
        return {"ok": True}

    dispatcher = ToolDispatcher({ok_tool.name: ok_tool})
    run = Run(run_id="r4", task="t")
    call = ToolCall(name="ok_tool", params={})

    result = await dispatcher.dispatch_with_retry(call, run)
    assert attempts == 1
    assert result.outcome is ToolOutcome.OK
    assert result.value == {"ok": True}
