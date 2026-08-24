from __future__ import annotations

import pytest

from prodagent import RunState
from prodagent.backends.file.checkpoint import FileCheckpointStore
from prodagent.kernel.events import RunCompletedEvent
from prodagent.kernel.loop import ReactiveLoop
from prodagent.kernel.state import AgentRun
from prodagent.kernel.types import LLMResponse, ToolCall
from prodagent.llm.fake import FakeLLMAdapter
from prodagent.tooling import tool
from prodagent.tooling.dispatcher import ToolDispatcher


def _final_run(events: list) -> AgentRun:
    for event in reversed(events):
        if isinstance(event, RunCompletedEvent):
            return event.run
    raise AssertionError("stream produced no RunCompletedEvent")


@tool(name="collect", readonly=True)
async def _collect_tool() -> dict:
    return {"result": "ran collect"}


def _make_loop(llm, store):
    dispatcher = ToolDispatcher({"collect": _collect_tool})
    return ReactiveLoop(
        llm,
        dispatcher,
        system_prompt="test",
        tools_schema=[],
        checkpoint_store=store,
    )


@pytest.mark.asyncio
async def test_checkpoint_written_each_turn(tmp_path):
    store = FileCheckpointStore(tmp_path)
    llm = FakeLLMAdapter(
        responses=[
            LLMResponse(content="", tool_calls=[ToolCall("collect", {})], stop_reason="tool_use"),
            LLMResponse(content="done", stop_reason="end_turn"),
        ]
    )
    loop = _make_loop(llm, store)
    streamed: list = []
    async for event in loop.stream("diagnose", run_id="run-A"):
        streamed.append(event)
    run = _final_run(streamed)

    assert run.state is RunState.COMPLETED
    saved = await store.load("run-A")
    assert saved is not None
    assert saved.messages


@pytest.mark.asyncio
async def test_resume_continues_from_checkpoint(tmp_path):
    store = FileCheckpointStore(tmp_path)

    class Boom(Exception):
        pass

    class CrashingLLM(FakeLLMAdapter):
        async def complete(self, *a, **k):
            if self._call_count >= 1:
                raise Boom("process died")
            return await super().complete(*a, **k)

    crashing = CrashingLLM(
        responses=[
            LLMResponse(content="", tool_calls=[ToolCall("collect", {})], stop_reason="tool_use"),
        ]
    )
    loop1 = _make_loop(crashing, store)
    with pytest.raises(Boom):
        async for _ in loop1.stream("diagnose", run_id="run-B"):
            pass

    mid = await store.load("run-B")
    assert mid is not None
    assert mid.messages
    turns_before = mid.turn_count

    healthy = FakeLLMAdapter(responses=[LLMResponse(content="done", stop_reason="end_turn")])
    loop2 = _make_loop(healthy, store)
    streamed2: list = []
    async for event in loop2.stream("diagnose", run_id="run-B"):
        streamed2.append(event)
    resumed = _final_run(streamed2)

    assert resumed.state is RunState.COMPLETED
    assert resumed.turn_count >= turns_before
    assert healthy.call_count == 1


@pytest.mark.asyncio
async def test_completed_run_is_not_resumed(tmp_path):
    store = FileCheckpointStore(tmp_path)
    done = AgentRun(run_id="run-C", task="x")
    done.state = RunState.COMPLETED
    done.messages = [{"role": "user", "content": "x"}]
    await store.save(done)

    llm = FakeLLMAdapter(responses=[LLMResponse(content="fresh", stop_reason="end_turn")])
    loop = _make_loop(llm, store)
    streamed3: list = []
    async for event in loop.stream("x", run_id="run-C"):
        streamed3.append(event)
    run = _final_run(streamed3)

    assert run.state is RunState.COMPLETED
    assert llm.call_count == 1
