from __future__ import annotations

import json
from typing import Any

import pytest

from prodagent.backends.file.checkpoint import FileCheckpointStore
from prodagent.backends.file.event_log import FileEventLog
from prodagent.kernel.events import RunCompletedEvent, RunFailedEvent, RunSuspendedEvent
from prodagent.kernel.state import AgentRun
from prodagent.kernel.types import LLMResponse
from prodagent.llm.fake import FakeLLMAdapter
from prodagent.plan.executor import PlanExecutor

_TERMINAL = (RunCompletedEvent, RunFailedEvent, RunSuspendedEvent)


def _run(run_id: str = "R1", *, plan_state: dict | None = None, plan_last_seq: int = 0) -> AgentRun:
    r = AgentRun(run_id=run_id, task="t")
    r.plan_state = plan_state
    r.plan_last_seq = plan_last_seq
    return r


def _final_run(events: list) -> Any:
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


class _RecordingExecutor:
    def __init__(self) -> None:
        self.calls: list[str] = []

    async def __call__(self, call, *, run_id: str = "") -> dict:
        self.calls.append(call.name)
        return {"status": "green", "action": call.name}


@pytest.mark.asyncio
async def test_save_produces_versioned_files_each_loadable(tmp_path):
    store = FileCheckpointStore(directory=tmp_path)
    r = _run("R1")
    await store.save(r)
    r.metrics.turn_count = 1
    await store.save(r)
    r.metrics.turn_count = 2
    await store.save(r)

    versions = await store.list_versions("R1")
    assert versions == [1, 2, 3]

    for n, expected_turns in [(1, 0), (2, 1), (3, 2)]:
        loaded = await store.load("R1", version=n)
        assert loaded is not None
        assert loaded.checkpoint_version == n
        assert loaded.turn_count == expected_turns

    latest = await store.load("R1")
    assert latest is not None
    assert latest.checkpoint_version == 3


@pytest.mark.asyncio
async def test_list_run_ids_excludes_lock_files(tmp_path):
    store = FileCheckpointStore(directory=tmp_path)
    await store.save(_run("A"))
    await store.save(_run("B"))
    ids = await store.list_run_ids()
    assert ids == ["A", "B"]


@pytest.mark.asyncio
async def test_fork_preserves_state_resets_plan_last_seq(tmp_path):
    store = FileCheckpointStore(directory=tmp_path)
    plan_state = {"version": 1, "steps": {"s1": {"step_id": "s1", "status": "completed"}}}
    r = _run("R1", plan_state=plan_state, plan_last_seq=7)
    await store.save(r)
    r.metrics.turn_count = 5
    await store.save(r)

    forked_id = await store.fork("R1", at_version=2)
    assert forked_id.startswith("R1:fork-v2-")

    forked = await store.load(forked_id)
    assert forked is not None
    assert forked.run_id == forked_id
    assert forked.plan_state == plan_state
    assert forked.plan_last_seq == 0, "plan_last_seq must reset so append doesn't VersionConflict"
    assert forked.turn_count == 5, "forked state must equal the v2 snapshot"
    assert forked.checkpoint_version == 1


@pytest.mark.asyncio
async def test_fork_with_explicit_new_run_id(tmp_path):
    store = FileCheckpointStore(directory=tmp_path)
    await store.save(_run("R1", plan_state={"version": 1, "steps": {}}))
    forked_id = await store.fork("R1", at_version=1, new_run_id="CUSTOM")
    assert forked_id == "CUSTOM"
    loaded = await store.load("CUSTOM")
    assert loaded is not None


@pytest.mark.asyncio
async def test_fork_refuses_existing_run_id(tmp_path):
    store = FileCheckpointStore(directory=tmp_path)
    await store.save(_run("R1"))
    await store.save(_run("TAKEN"))
    from prodagent import VersionConflict

    with pytest.raises(VersionConflict):
        await store.fork("R1", at_version=1, new_run_id="TAKEN")


@pytest.mark.asyncio
async def test_forked_run_resumes_without_planner_and_without_version_conflict(tmp_path):
    events = FileEventLog(tmp_path / "events")
    checkpoints = FileCheckpointStore(directory=tmp_path / "checkpoints")

    executor = _RecordingExecutor()
    planner = PlanExecutor(
        _plan_llm(_two_step_plan()),
        executor,
        system="sys",
        messages=[{"role": "user", "content": "do"}],
        event_log=events,
        checkpoint_store=checkpoints,
    )
    streamed: list = []
    async for event in planner.stream("do the thing", run_id="R1"):
        streamed.append(event)
    assert _final_run(streamed).state.value == "completed"
    assert executor.calls == ["collect", "report"]

    versions = await checkpoints.list_versions("R1")
    assert len(versions) >= 2
    fork_at = versions[-2]

    forked_id = await checkpoints.fork("R1", at_version=fork_at)

    executor2 = _RecordingExecutor()
    sentinel_llm = _plan_llm({"steps": [{"id": "X", "action": "must_not", "depends_on": []}]})
    planner2 = PlanExecutor(
        sentinel_llm,
        executor2,
        system="sys",
        messages=[{"role": "user", "content": "resume"}],
        event_log=events,
        checkpoint_store=checkpoints,
    )
    streamed2: list = []
    async for event in planner2.stream("resume from fork", run_id=forked_id):
        streamed2.append(event)

    assert sentinel_llm.call_count == 0, (
        "forked run must restore from plan_state, not call the planner LLM"
    )
    assert any(isinstance(e, _TERMINAL) for e in streamed2), "forked run must terminate cleanly"
