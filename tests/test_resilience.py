"""Step-level resilience: timeout, retry with backoff, cancellation is never retried."""

import asyncio

import pytest

from src import Workflow
from src.kernel import RetryPolicy


async def test_retry_until_success():
    calls = {"n": 0}

    async def flaky(_, ctx):
        calls["n"] += 1
        if calls["n"] < 3:
            raise RuntimeError("临时故障")
        return f"第{calls['n']}次成功"

    wf = Workflow()
    wf.add_node("n", flaky, terminal=True, retry=RetryPolicy(max_attempts=3, base_delay=0))
    wf.entry("n")
    r = await wf.run("")
    assert r.status == "completed"
    assert r.output == "第3次成功"
    assert calls["n"] == 3


async def test_retry_exhausted_fails_run():
    calls = {"n": 0}

    async def always_fail(_, ctx):
        calls["n"] += 1
        raise RuntimeError("一直坏")

    wf = Workflow()
    wf.add_node("n", always_fail, terminal=True, retry=RetryPolicy(max_attempts=2, base_delay=0))
    wf.entry("n")
    r = await wf.run("")
    assert r.status == "failed"
    assert calls["n"] == 2  # first attempt + one retry, then give up


async def test_timeout_counts_as_one_attempt():
    calls = {"n": 0}

    async def slow(_, ctx):
        calls["n"] += 1
        await asyncio.sleep(0.3)
        return "不该返回"

    wf = Workflow()
    wf.add_node(
        "n", slow, terminal=True, timeout=0.05, retry=RetryPolicy(max_attempts=2, base_delay=0)
    )
    wf.entry("n")
    loop = asyncio.get_event_loop()
    t0 = loop.time()
    r = await wf.run("")
    assert r.status == "failed"
    assert calls["n"] == 2  # each timeout counts as one failure and triggers a retry
    assert loop.time() - t0 < 0.25  # two 0.05s timeouts, far less than 0.3


async def test_cancellation_is_never_retried():
    calls = {"n": 0}

    async def cancelled(_, ctx):
        calls["n"] += 1
        raise asyncio.CancelledError()

    wf = Workflow()
    wf.add_node("n", cancelled, terminal=True, retry=RetryPolicy(max_attempts=3, base_delay=0))
    wf.entry("n")
    with pytest.raises(asyncio.CancelledError):
        await wf.run("")
    assert calls["n"] == 1  # external cancellation must propagate unchanged, never retried
