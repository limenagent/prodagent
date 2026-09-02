from __future__ import annotations

import json

import pytest

from prodagent.backends.file.checkpoint import FileCheckpointStore
from prodagent.backends.file.event_log import FileEventLog
from prodagent.kernel.bodies.runner import BodyRunner
from prodagent.kernel.types import (
    LLMResponse,
    NodeCompletedEvent,
    NodeFailedEvent,
    NodeStartedEvent,
    RunCompletedEvent,
    RunFailedEvent,
    RunSuspendedEvent,
)
from prodagent.llm.fake import FakeLLMAdapter
from prodagent.plan.scheduler import Scheduler

_TERMINAL = (RunCompletedEvent, RunFailedEvent, RunSuspendedEvent)


def _final_run(events: list) -> object:
    for event in reversed(events):
        if isinstance(event, _TERMINAL):
            return event.run
    raise AssertionError("stream produced no terminal event")


def _plan_llm(*plans: dict) -> FakeLLMAdapter:
    return FakeLLMAdapter(
        responses=[LLMResponse(content=json.dumps(p), stop_reason="end_turn") for p in plans]
    )


def _two_step_plan() -> dict:
    return {
        "steps": [
            {"id": "s1", "action": "collect", "params": {}, "depends_on": []},
            {"id": "s2", "action": "report", "params": {}, "depends_on": ["s1"]},
        ]
    }


def _stores(tmp_path):
    return (
        FileEventLog(tmp_path / "events"),
        FileCheckpointStore(directory=tmp_path / "checkpoints"),
    )


class _RecordingExecutor:
    def __init__(self) -> None:
        self.calls: list[str] = []

    async def __call__(self, call, *, run_id: str = "") -> dict:
        self.calls.append(call.name)
        return {"status": "green", "action": call.name}


@pytest.mark.asyncio
async def test_stream_yields_step_events_in_order(tmp_path):
    events, checkpoints = _stores(tmp_path)
    executor = _RecordingExecutor()
    planner = Scheduler(
        _plan_llm(_two_step_plan()),
        BodyRunner(tools=executor),
        system="sys",
        initial_messages=[{"role": "user", "content": "do"}],
        event_log=events,
        checkpoint_store=checkpoints,
    )

    streamed: list = []
    async for event in planner.stream("do the thing", run_id="R1"):
        streamed.append(event)

    assert len(streamed) == 5
    assert isinstance(streamed[0], NodeStartedEvent)
    assert streamed[0].node_id == "s1"
    assert isinstance(streamed[1], NodeCompletedEvent)
    assert streamed[1].node_id == "s1"
    assert isinstance(streamed[2], NodeStartedEvent)
    assert streamed[2].node_id == "s2"
    assert isinstance(streamed[3], NodeCompletedEvent)
    assert streamed[3].node_id == "s2"
    assert isinstance(streamed[4], RunCompletedEvent)
    for e in streamed:
        rid = getattr(e, "run_id", None) or getattr(e.run, "run_id", None)
        assert rid == "R1"
    assert executor.calls == ["collect", "report"]


@pytest.mark.asyncio
async def test_stream_terminal_event_carries_finalised_run(tmp_path):
    events, checkpoints = _stores(tmp_path)
    planner = Scheduler(
        _plan_llm(_two_step_plan()),
        BodyRunner(tools=_RecordingExecutor()),
        system="sys",
        initial_messages=[{"role": "user", "content": "do"}],
        event_log=events,
        checkpoint_store=checkpoints,
    )

    streamed: list = []
    async for event in planner.stream("do the thing", run_id="R2"):
        streamed.append(event)

    run = _final_run(streamed)
    assert run.state.value == "completed"
    assert run.run_id == "R2"


@pytest.mark.asyncio
async def test_stream_yields_step_failed_on_tool_error(tmp_path):
    events, checkpoints = _stores(tmp_path)

    class _FailingExecutor:
        async def __call__(self, call, *, run_id: str = "") -> dict:
            raise RuntimeError("boom")

    planner = Scheduler(
        _plan_llm(_two_step_plan(), {"steps": []}),
        BodyRunner(tools=_FailingExecutor()),
        system="sys",
        initial_messages=[{"role": "user", "content": "do"}],
        event_log=events,
        checkpoint_store=checkpoints,
        max_replans=1,
    )

    streamed: list = []
    async for event in planner.stream("do the thing", run_id="R3"):
        streamed.append(event)

    started = [e for e in streamed if isinstance(e, NodeStartedEvent)]
    failed = [e for e in streamed if isinstance(e, NodeFailedEvent)]
    assert started, "expected at least one NodeStartedEvent"
    assert failed, "expected at least one NodeFailedEvent"
    assert "boom" in failed[0].error


@pytest.mark.asyncio
async def test_stream_parallel_steps_yield_started_before_completed(tmp_path):
    events, checkpoints = _stores(tmp_path)

    parallel_plan = {
        "steps": [
            {"id": "a", "action": "fetch", "params": {}, "depends_on": []},
            {"id": "b", "action": "fetch", "params": {}, "depends_on": []},
        ]
    }
    planner = Scheduler(
        _plan_llm(parallel_plan),
        BodyRunner(tools=_RecordingExecutor()),
        system="sys",
        initial_messages=[{"role": "user", "content": "do"}],
        event_log=events,
        checkpoint_store=checkpoints,
    )

    streamed: list = []
    async for event in planner.stream("do the thing", run_id="R4"):
        streamed.append(event)

    assert isinstance(streamed[0], NodeStartedEvent)
    assert isinstance(streamed[1], NodeStartedEvent)
    assert isinstance(streamed[2], NodeCompletedEvent)
    assert isinstance(streamed[3], NodeCompletedEvent)
    assert isinstance(streamed[4], RunCompletedEvent)


@pytest.mark.asyncio
async def test_stream_llm_call_failure_marks_run_failed(tmp_path):

    class _FailingLLM:
        async def complete(self, messages, *, system="", tools=None, config=None, on_chunk):
            raise RuntimeError("anthropic 503")

    events, checkpoints = _stores(tmp_path)
    planner = Scheduler(
        _FailingLLM(),  # type: ignore[arg-type]
        _RecordingExecutor(),
        system="sys",
        initial_messages=[{"role": "user", "content": "do"}],
        event_log=events,
        checkpoint_store=checkpoints,
    )

    streamed: list = []
    async for event in planner.stream("do the thing", run_id="R-llm-fail"):
        streamed.append(event)

    assert isinstance(streamed[-1], RunFailedEvent)
    run = streamed[-1].run
    assert run.state.value == "failed"
    assert "503" in run.last_error, f"last_error should carry the LLM error: {run.last_error!r}"
