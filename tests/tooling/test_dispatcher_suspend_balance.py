"""A batch that ends early (suspension) must leave a wire-valid transcript.

The assistant message carrying N tool_calls is already on the run when
``run_batch`` starts. If one call suspends, the never-dispatched siblings
would have a tool_use with no tool_result — the next provider request 400s
on APIs that require strict pairing (Anthropic). Regression: balance the
transcript with explicit skip markers and keep tool_history = what ran.
"""

from __future__ import annotations

from typing import Any

from prodagent.kernel.run import Run
from prodagent.kernel.types import Message, SideEffectLevel, ToolCall, ToolMeta
from prodagent.tooling.base import FunctionTool
from prodagent.tooling.dispatcher import ToolDispatcher

EXECUTED: list[str] = []


def _write_tool(name: str, ret: Any, *, readonly: bool = False) -> FunctionTool:
    async def fn(**_: Any) -> Any:
        EXECUTED.append(name)
        return ret

    return FunctionTool(
        name=name,
        fn=fn,
        meta=ToolMeta(
            name=name,
            is_readonly=readonly,
            side_effect_level=SideEffectLevel.LOW if readonly else SideEffectLevel.MEDIUM,
        ),
        schema={
            "name": name,
            "description": name,
            "parameters": {"type": "object", "properties": {}},
        },
    )


def _run_with_assistant_batch(call_ids: list[str]) -> Run:
    run = Run(run_id="r-balance", task="t")
    msg: Message = {
        "role": "assistant",
        "content": "",
        "tool_calls": [
            {"id": cid, "type": "function", "function": {"name": f"tool_{i}", "arguments": "{}"}}
            for i, cid in enumerate(call_ids)
        ],
    }
    run.messages.append(msg)
    return run


def _tool_result_ids(run: Run) -> set[str]:
    return {
        m.get("tool_call_id", "")
        for m in run.messages
        if m.get("role") == "tool" and m.get("tool_call_id")
    }


async def test_mid_batch_suspension_balances_transcript_and_skips_siblings():
    EXECUTED.clear()
    suspend = _write_tool("tool_0", {"suspended": True, "reason": "await approval"})
    sibling = _write_tool("tool_1", {"ok": True})
    dispatcher = ToolDispatcher({"tool_0": suspend, "tool_1": sibling})

    run = _run_with_assistant_batch(["c0", "c1"])
    batch = [
        ToolCall(name="tool_0", params={}, call_id="c0"),
        ToolCall(name="tool_1", params={}, call_id="c1"),
    ]

    async for _ in dispatcher.run_batch(run, batch):
        pass

    # The sibling never executed and neither stayed in tool_history —
    # history records what actually ran; the suspended call is replayed later.
    assert EXECUTED == ["tool_0"]
    assert [c.call_id for c in run.tool_history] == []
    assert run.interrupt is not None
    assert run.interrupt.staged_call() is not None
    assert run.interrupt.staged_call().call_id == "c0"

    # Every tool_use in the assistant batch now has a paired tool_result:
    # c0's placeholder is absent (replayed on resume), c1 got a skip marker.
    result_ids = _tool_result_ids(run)
    assert result_ids == {"c1"}, result_ids
    skip = next(m for m in run.messages if m.get("tool_call_id") == "c1")
    assert "skipped" in str(skip["content"])


async def test_mid_batch_handoff_balances_transcript():
    EXECUTED.clear()
    handoff = _write_tool(
        "tool_0", {"handoff": True, "peer": "reviewer", "task": "review the plan"}, readonly=True
    )
    sibling = _write_tool("tool_1", {"ok": True})
    dispatcher = ToolDispatcher({"tool_0": handoff, "tool_1": sibling})

    run = _run_with_assistant_batch(["c0", "c1"])
    batch = [
        ToolCall(name="tool_0", params={}, call_id="c0"),
        ToolCall(name="tool_1", params={}, call_id="c1"),
    ]

    async for _ in dispatcher.run_batch(run, batch):
        pass

    # Nothing parks: control transfer is a command the scheduler applies.
    # The handoff call is answered inline (no dangling tool_use); the
    # sibling is skip-marked.
    assert _tool_result_ids(run) == {"c0", "c1"}
    answered = next(m for m in run.messages if m.get("tool_call_id") == "c0")
    assert "reviewer" in str(answered["content"])
    assert [c.call_id for c in run.tool_history] == []
