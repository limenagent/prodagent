"""REACTIVE per-turn crash recovery — the counterpart to
``test_plan_crash_recovery_e2e.py`` for the non-plan execution mode.

Before ``ReactiveLoop`` grew ``_record_turn``, the only checkpoint write in
``stream()`` sat in a ``finally`` at the very end of the call — a real
process kill mid-hop (not a raised Python exception, which that ``finally``
already survives) loses every turn since the hop started. With an
``event_log`` configured, each turn is checkpointed as it completes, so a
kill mid-turn N loses at most turn N, never the turns before it.
"""

from __future__ import annotations

import pytest

from prodagent import RunState
from prodagent.backends.file.checkpoint import FileCheckpointStore
from prodagent.backends.file.event_log import FileEventLog
from prodagent.core.event_log import RunEventType
from prodagent.kernel.bus import HookEvent, HookRegistry
from prodagent.kernel.events import RunCompletedEvent
from prodagent.kernel.loop import ReactiveLoop
from prodagent.kernel.types import LLMResponse, ToolCall
from prodagent.llm.fake import FakeLLMAdapter
from prodagent.tooling import tool
from prodagent.tooling.dispatcher import ToolDispatcher


@tool(name="collect", readonly=True)
async def _collect_tool() -> dict:
    return {"result": "ran collect"}


def _final_run(events: list):
    for event in reversed(events):
        if isinstance(event, RunCompletedEvent):
            return event.run
    raise AssertionError("stream produced no RunCompletedEvent")


@pytest.mark.asyncio
async def test_mid_hop_kill_loses_at_most_one_turn(tmp_path):
    events = FileEventLog(tmp_path / "events")
    store = FileCheckpointStore(tmp_path / "checkpoints")

    turn_starts = 0

    async def _count_turn_start(**_: object) -> None:
        nonlocal turn_starts
        turn_starts += 1

    hooks = HookRegistry()
    hooks.register_event(HookEvent.TURN_START, _count_turn_start)

    llm = FakeLLMAdapter(
        responses=[
            LLMResponse(content="", tool_calls=[ToolCall("collect", {})], stop_reason="tool_use"),
            LLMResponse(content="", tool_calls=[ToolCall("collect", {})], stop_reason="tool_use"),
            LLMResponse(content="done", stop_reason="end_turn"),
        ]
    )
    dispatcher = ToolDispatcher({"collect": _collect_tool})
    loop = ReactiveLoop(
        llm,
        dispatcher,
        system_prompt="test",
        tools_schema=[],
        checkpoint_store=store,
        event_log=events,
        hooks=hooks,
    )

    # "Crash" after turn 1 is fully recorded but before turn 2 finishes: stop
    # pulling from the stream the moment turn 2 starts, and never call
    # `aclose()` — a real SIGKILL never runs `stream()`'s `finally` either.
    gen = loop.stream("diagnose", run_id="run-X")
    async for _event in gen:
        if turn_starts >= 2:
            break

    mid = await store.load("run-X")
    assert mid is not None, "per-turn checkpoint must have captured turn 1 before the kill"
    assert mid.turn_count == 1, "turn 2 died mid-flight and must not be reflected in the checkpoint"

    recorded_types = [e.event_type for e in await events.get_events("run-X")]
    assert recorded_types.count(RunEventType.TURN_COMPLETED) == 1

    llm2 = FakeLLMAdapter(
        responses=[
            LLMResponse(content="", tool_calls=[ToolCall("collect", {})], stop_reason="tool_use"),
            LLMResponse(content="done", stop_reason="end_turn"),
        ]
    )
    dispatcher2 = ToolDispatcher({"collect": _collect_tool})
    loop2 = ReactiveLoop(
        llm2,
        dispatcher2,
        system_prompt="test",
        tools_schema=[],
        checkpoint_store=store,
        event_log=events,
    )
    streamed: list = []
    async for event in loop2.stream("diagnose", run_id="run-X"):
        streamed.append(event)
    resumed = _final_run(streamed)

    assert resumed.state is RunState.COMPLETED
    assert resumed.turn_count == 3, "1 committed turn + 2 replayed turns after resume"
    assert llm2.call_count == 2, "resume must continue from turn 2, not replay turn 1"


@pytest.mark.asyncio
async def test_unconfigured_event_log_keeps_legacy_single_checkpoint_behavior(tmp_path):
    """No ``event_log`` passed in — must behave exactly like before this
    change: no per-turn writes, only the final checkpoint in ``finally``."""
    store = FileCheckpointStore(tmp_path)
    llm = FakeLLMAdapter(
        responses=[
            LLMResponse(content="", tool_calls=[ToolCall("collect", {})], stop_reason="tool_use"),
            LLMResponse(content="done", stop_reason="end_turn"),
        ]
    )
    dispatcher = ToolDispatcher({"collect": _collect_tool})
    loop = ReactiveLoop(
        llm,
        dispatcher,
        system_prompt="test",
        tools_schema=[],
        checkpoint_store=store,
    )
    streamed: list = []
    async for event in loop.stream("diagnose", run_id="run-Y"):
        streamed.append(event)
    resumed = _final_run(streamed)

    assert resumed.state is RunState.COMPLETED
    saved = await store.load("run-Y")
    assert saved is not None
    assert saved.turn_count == 2
