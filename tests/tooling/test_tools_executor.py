from __future__ import annotations

import pytest

from prodagent import RunState, SideEffectLevel, ToolMeta
from prodagent.core.events import ToolResultEvent
from prodagent.core.state import AgentRun
from prodagent.core.types import ToolCall
from prodagent.tooling import tool
from prodagent.tooling.dispatcher import ToolDispatcher
from prodagent.tooling.runner import ToolRunner


def _dispatcher(*tools_list) -> ToolDispatcher:
    return ToolDispatcher({t.name: t for t in tools_list})


async def _run_batch(runner: ToolRunner, run: AgentRun, calls: list[ToolCall]) -> list[dict]:
    results = []
    async for event in runner.run_batch(run, calls):
        if isinstance(event, ToolResultEvent):
            assert hasattr(event.result, "to_wire"), "ToolResultEvent.result must be a ToolResult"
            results.append(event.result.to_wire())
    return results


@pytest.mark.asyncio
async def test_parallel_readonly_all_returned():
    @tool(name="read_a", readonly=True)
    async def read_a() -> dict:
        return {"a": 1}

    @tool(name="read_b", readonly=True)
    async def read_b() -> dict:
        return {"b": 2}

    run = AgentRun(run_id="r1", task="test")
    calls = [ToolCall(name="read_a", params={}), ToolCall(name="read_b", params={})]
    runner = ToolRunner(_dispatcher(read_a, read_b))

    results = await _run_batch(runner, run, calls)
    assert len(results) == 2
    assert {"a": 1} in results
    assert {"b": 2} in results


@pytest.mark.asyncio
async def test_serial_write_order_preserved():
    order: list[str] = []

    @tool(
        name="write_x",
        meta=ToolMeta(name="write_x", side_effect_level=SideEffectLevel.MEDIUM),
    )
    async def write_x() -> dict:
        order.append("x")
        return {"wrote": "x"}

    @tool(
        name="write_y",
        meta=ToolMeta(name="write_y", side_effect_level=SideEffectLevel.MEDIUM),
    )
    async def write_y() -> dict:
        order.append("y")
        return {"wrote": "y"}

    run = AgentRun(run_id="r2", task="test")
    calls = [ToolCall(name="write_x", params={}), ToolCall(name="write_y", params={})]
    runner = ToolRunner(_dispatcher(write_x, write_y))

    results = await _run_batch(runner, run, calls)
    assert len(results) == 2
    assert order == ["x", "y"]


@pytest.mark.asyncio
async def test_high_tool_suspends_without_approval_bundle():

    @tool(
        name="page_oncall",
        meta=ToolMeta(name="page_oncall", side_effect_level=SideEffectLevel.HIGH),
    )
    async def page_oncall(team: str) -> dict:  # pragma: no cover — must not run
        return {"paged": team}

    run = AgentRun(run_id="r3", task="test")
    calls = [ToolCall(name="page_oncall", params={"team": "platform"})]
    runner = ToolRunner(_dispatcher(page_oncall))

    await _run_batch(runner, run, calls)

    assert run.state == RunState.SUSPENDED
    assert run.pending_tool_call is not None
    assert run.pending_tool_call.name == "page_oncall"
    assert not any(c.name == "page_oncall" for c in run.tool_history)


@pytest.mark.asyncio
async def test_high_tool_suspends_leaves_no_tool_result():

    @tool(
        name="critical_op",
        meta=ToolMeta(name="critical_op", side_effect_level=SideEffectLevel.HIGH),
    )
    async def critical_op() -> dict:  # pragma: no cover — must not run
        return {"done": True}

    run = AgentRun(run_id="r4", task="test")
    calls = [ToolCall(name="critical_op", params={})]
    runner = ToolRunner(_dispatcher(critical_op))

    yielded_results: list[dict] = []
    async for event in runner.run_batch(run, calls):
        if isinstance(event, ToolResultEvent):
            yielded_results.append(event.result.to_wire())

    assert len(yielded_results) == 1
    assert yielded_results[0].get("suspended") is True
    assert yielded_results[0].get("tool") == "critical_op"
    assert run.state == RunState.SUSPENDED
    assert run.pending_tool_call is not None


@pytest.mark.asyncio
async def test_mixed_reads_and_writes():
    @tool(name="read_only", readonly=True)
    async def read_only() -> dict:
        return {"type": "read"}

    @tool(
        name="do_write",
        meta=ToolMeta(name="do_write", side_effect_level=SideEffectLevel.MEDIUM),
    )
    async def do_write() -> dict:
        return {"type": "write"}

    run = AgentRun(run_id="r5", task="test")
    calls = [
        ToolCall(name="read_only", params={}),
        ToolCall(name="do_write", params={}),
    ]
    runner = ToolRunner(_dispatcher(read_only, do_write))

    results = await _run_batch(runner, run, calls)
    assert len(results) == 2
    assert {"type": "read"} in results
    assert {"type": "write"} in results


@pytest.mark.asyncio
async def test_high_tool_does_not_drop_prior_readonly_results():

    @tool(name="safe_read", readonly=True)
    async def safe_read() -> dict:
        return {"type": "read", "value": 42}

    @tool(
        name="dangerous_write",
        meta=ToolMeta(name="dangerous_write", side_effect_level=SideEffectLevel.HIGH),
    )
    async def dangerous_write() -> dict:  # pragma: no cover — must not run
        return {}

    run = AgentRun(run_id="r6", task="test")
    calls = [
        ToolCall(name="safe_read", params={}),
        ToolCall(name="dangerous_write", params={}),
    ]
    runner = ToolRunner(_dispatcher(safe_read, dangerous_write))

    yielded_results: list[dict] = []
    async for event in runner.run_batch(run, calls):
        if isinstance(event, ToolResultEvent):
            yielded_results.append(event.result.to_wire())

    assert any(r == {"type": "read", "value": 42} for r in yielded_results), (
        f"readonly result dropped on SUSPEND: {yielded_results}"
    )
    assert run.state == RunState.SUSPENDED
    assert run.pending_tool_call is not None
    assert run.pending_tool_call.name == "dangerous_write"
    assert not any(c.name == "dangerous_write" for c in run.tool_history)
    assert any("42" in m.get("content", "") for m in run.messages if m.get("role") == "tool")


@pytest.mark.asyncio
async def test_high_tool_first_still_allows_later_readonly():

    @tool(name="safe_read", readonly=True)
    async def safe_read() -> dict:
        return {"type": "read"}

    @tool(
        name="dangerous_write",
        meta=ToolMeta(name="dangerous_write", side_effect_level=SideEffectLevel.HIGH),
    )
    async def dangerous_write() -> dict:  # pragma: no cover
        return {}

    run = AgentRun(run_id="r7", task="test")
    calls = [
        ToolCall(name="dangerous_write", params={}),
        ToolCall(name="safe_read", params={}),
    ]
    runner = ToolRunner(_dispatcher(safe_read, dangerous_write))

    yielded_results: list[dict] = []
    async for event in runner.run_batch(run, calls):
        if isinstance(event, ToolResultEvent):
            yielded_results.append(event.result.to_wire())

    assert {"type": "read"} in yielded_results
    assert run.state == RunState.SUSPENDED
    assert not any(c.name == "dangerous_write" for c in run.tool_history)


@pytest.mark.asyncio
async def test_readonly_concurrency_cap_enforced():
    import asyncio

    from prodagent.core.config import LoopConfig

    in_flight = 0
    max_in_flight = 0
    lock = asyncio.Lock()

    @tool(name="probe", readonly=True)
    async def probe() -> dict:
        nonlocal in_flight, max_in_flight
        async with lock:
            in_flight += 1
            max_in_flight = max(max_in_flight, in_flight)
        await asyncio.sleep(0.01)
        async with lock:
            in_flight -= 1
        return {"ok": True}

    run = AgentRun(run_id="rcap", task="test")
    calls = [ToolCall(name="probe", params={}) for _ in range(20)]
    runner = ToolRunner(
        _dispatcher(probe),
        loop_config=LoopConfig(readonly_concurrency=4, repeat_threshold=100),
    )

    results = await _run_batch(runner, run, calls)
    assert len(results) == 20
    assert max_in_flight <= 4, f"concurrency cap violated: max_in_flight={max_in_flight} > 4"
    assert max_in_flight >= 2, f"concurrency too conservative: max_in_flight={max_in_flight} < 2"


@pytest.mark.asyncio
async def test_readonly_concurrency_default_8_when_no_loop_config():
    import asyncio

    from prodagent.core.config import LoopConfig

    in_flight = 0
    max_in_flight = 0
    lock = asyncio.Lock()

    @tool(name="probe2", readonly=True)
    async def probe2() -> dict:
        nonlocal in_flight, max_in_flight
        async with lock:
            in_flight += 1
            max_in_flight = max(max_in_flight, in_flight)
        await asyncio.sleep(0.01)
        async with lock:
            in_flight -= 1
        return {"ok": True}

    run = AgentRun(run_id="rcap-default", task="test")
    calls = [ToolCall(name="probe2", params={}) for _ in range(20)]
    runner = ToolRunner(
        _dispatcher(probe2),
        loop_config=LoopConfig(repeat_threshold=100),
    )

    await _run_batch(runner, run, calls)
    assert max_in_flight <= 8, f"default cap violated: max_in_flight={max_in_flight} > 8"


@pytest.mark.asyncio
async def test_enforced_idempotent_injects_key_bound_to_call_site():

    captured: list[dict] = []

    @tool(
        name="refund_order",
        meta=ToolMeta(
            name="refund_order",
            side_effect_level=SideEffectLevel.MEDIUM,
            enforced_idempotent=True,
        ),
    )
    async def refund_order(order_id: str, idempotency_key: str = "") -> dict:
        captured.append({"order_id": order_id, "idempotency_key": idempotency_key})
        return {"refunded": order_id}

    run = AgentRun(run_id="run-xyz", task="refund")
    run.metrics.turn_count = 3
    calls = [
        ToolCall(name="refund_order", params={"order_id": "A1"}),
        ToolCall(name="refund_order", params={"order_id": "A2"}),
    ]
    runner = ToolRunner(_dispatcher(refund_order))

    await _run_batch(runner, run, calls)

    assert len(captured) == 2
    # Keys anchor on the persisted idempotency_seq (turn-independent), so a
    # call re-executed after a crash-restore re-derives the same key.
    assert captured[0]["idempotency_key"] == "run-xyz:c1"
    assert captured[1]["idempotency_key"] == "run-xyz:c2"
    assert run.idempotency_seq == 2


@pytest.mark.asyncio
async def test_rollback_restore_rederives_same_idempotency_keys():
    """Crash mid-turn: the checkpoint (taken before the batch) rolls the seq
    back with the transcript, so re-executing the batch re-derives the same
    keys — the external system suppresses the duplicate side effect."""

    captured: list[dict] = []

    @tool(
        name="refund_order",
        meta=ToolMeta(
            name="refund_order",
            side_effect_level=SideEffectLevel.MEDIUM,
            enforced_idempotent=True,
        ),
    )
    async def refund_order(order_id: str, idempotency_key: str = "") -> dict:
        captured.append({"order_id": order_id, "idempotency_key": idempotency_key})
        return {"refunded": order_id}

    async def _drive(run: AgentRun) -> list[str]:
        captured.clear()
        calls = [
            ToolCall(name="refund_order", params={"order_id": "A1"}),
            ToolCall(name="refund_order", params={"order_id": "A2"}),
        ]
        runner = ToolRunner(_dispatcher(refund_order))
        await _run_batch(runner, run, calls)
        return [c["idempotency_key"] for c in captured]

    run = AgentRun(run_id="rb", task="refund")
    checkpoint = run.to_dict()  # saved before the side-effect batch

    first = await _drive(run)
    restored = AgentRun.from_dict(checkpoint)
    second = await _drive(restored)

    assert first == ["rb:c1", "rb:c2"]
    assert second == first


@pytest.mark.asyncio
async def test_enforced_idempotent_does_not_overwrite_model_supplied_key():

    captured: list[str] = []

    @tool(
        name="charge_card",
        meta=ToolMeta(
            name="charge_card",
            side_effect_level=SideEffectLevel.MEDIUM,
            enforced_idempotent=True,
        ),
    )
    async def charge_card(idempotency_key: str = "") -> dict:
        captured.append(idempotency_key)
        return {"ok": True}

    run = AgentRun(run_id="r-key", task="charge")
    calls = [ToolCall(name="charge_card", params={"idempotency_key": "client-supplied"})]
    runner = ToolRunner(_dispatcher(charge_card))

    await _run_batch(runner, run, calls)
    assert captured == ["client-supplied"]


@pytest.mark.asyncio
async def test_non_enforced_tool_gets_no_key():

    captured: list[dict] = []

    @tool(name="ping", readonly=True)
    async def ping(idempotency_key: str = "") -> dict:
        captured.append({"idempotency_key": idempotency_key})
        return {"pong": True}

    run = AgentRun(run_id="r-ping", task="ping")
    calls = [ToolCall(name="ping", params={})]
    runner = ToolRunner(_dispatcher(ping))

    await _run_batch(runner, run, calls)
    assert captured == [{"idempotency_key": ""}]


def _assert_consecutive_tool_results(messages):
    """Mirror the LLM API contract: every assistant tool_calls message must be
    followed immediately by consecutive tool messages covering each call_id."""
    n = len(messages)
    i = 0
    while i < n:
        m = messages[i]
        if m.get("role") == "assistant" and m.get("tool_calls"):
            expected = [tc.get("id") for tc in m["tool_calls"]]
            j = i + 1
            answered = []
            while j < n and messages[j].get("role") == "tool":
                answered.append(messages[j].get("tool_call_id"))
                j += 1
            assert answered == expected, (
                f"tool messages not consecutive/complete after assistant: "
                f"expected {expected}, got {answered}"
            )
            i = j
        else:
            i += 1


@pytest.mark.asyncio
async def test_skill_injection_deferred_after_all_tool_results():
    """Regression: get_skill's SKILL_INJECTION_KEY used to append a user
    message after EACH tool result, interleaving [tool, user, tool, user].
    The LLM API requires sibling tool results to be consecutive after the
    assistant tool_calls message → HTTP 400. Injections must be deferred to
    the end of the batch."""
    from prodagent.core.types import SKILL_INJECTION_KEY

    @tool(name="load_skill", readonly=True)
    async def load_skill(skill: str) -> dict:
        return {SKILL_INJECTION_KEY: f"[SKILL: {skill}] full doc", "skill": skill, "loaded": True}

    run = AgentRun(run_id="r-skill", task="load skills")
    calls = [
        ToolCall(name="load_skill", params={"skill": "alpha"}),
        ToolCall(name="load_skill", params={"skill": "beta"}),
    ]
    runner = ToolRunner(_dispatcher(load_skill))

    await _run_batch(runner, run, calls)

    roles = [m.get("role") for m in run.messages]
    assert roles == ["tool", "tool", "user", "user"], f"unexpected order: {roles}"
    assert run.messages[0]["tool_call_id"] == calls[0].call_id
    assert run.messages[1]["tool_call_id"] == calls[1].call_id
    assert str(run.messages[2]["content"]).startswith("[SKILL: alpha]")
    assert str(run.messages[3]["content"]).startswith("[SKILL: beta]")

    # End-to-end shape: assistant(tool_calls=[A,B]) → tool(A), tool(B) → injections.
    full = [
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {
                    "id": c.call_id,
                    "type": "function",
                    "function": {"name": "load_skill", "arguments": "{}"},
                }
                for c in calls
            ],
        },
        *run.messages,
    ]
    _assert_consecutive_tool_results(full)
