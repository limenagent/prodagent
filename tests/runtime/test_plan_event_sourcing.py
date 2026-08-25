from __future__ import annotations

import json
from typing import Any

import pytest

from prodagent.backends.file.checkpoint import FileCheckpointStore
from prodagent.backends.file.event_log import FileEventLog
from prodagent.core.event_log import Event, PlanEventType
from prodagent.kernel.events import RunCompletedEvent, RunFailedEvent, RunSuspendedEvent
from prodagent.kernel.types import LLMResponse
from prodagent.llm.fake import FakeLLMAdapter
from prodagent.plan.executor import PlanExecutor

_TERMINAL = (RunCompletedEvent, RunFailedEvent, RunSuspendedEvent)


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
async def test_emits_event_sequence(tmp_path):
    events, checkpoints = _stores(tmp_path)
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
    run = _final_run(streamed)

    types = [e.event_type for e in await events.get_events("R1")]
    assert types == [
        PlanEventType.PLAN_CREATED,
        PlanEventType.STEP_STARTED,
        PlanEventType.STEP_COMPLETED,
        PlanEventType.STEP_STARTED,
        PlanEventType.STEP_COMPLETED,
    ]
    assert executor.calls == ["collect", "report"]
    saved = await checkpoints.load("R1")
    assert saved is not None
    assert saved.plan_last_seq == (await events.get_events("R1"))[-1].seq
    assert run.state.value == "completed"


@pytest.mark.asyncio
async def test_resume_skips_completed(tmp_path):
    events, checkpoints = _stores(tmp_path)
    await events.append(
        Event.make(
            PlanEventType.PLAN_CREATED,
            "R2",
            version=1,
            steps=[
                {"step_id": "s1", "action": "collect", "depends_on": [], "status": "pending"},
                {"step_id": "s2", "action": "report", "depends_on": ["s1"], "status": "pending"},
            ],
        )
    )
    await events.append(Event.make(PlanEventType.STEP_STARTED, "R2", version=1, step_id="s1"))
    await events.append(
        Event.make(
            PlanEventType.STEP_COMPLETED, "R2", version=1, step_id="s1", output_ref={"ok": 1}
        )
    )

    executor = _RecordingExecutor()
    llm = _plan_llm(_two_step_plan())
    planner = PlanExecutor(
        llm,
        executor,
        system="sys",
        messages=[{"role": "user", "content": "resume"}],
        event_log=events,
        checkpoint_store=checkpoints,
    )

    async for _ in planner.stream("resume", run_id="R2"):
        pass

    assert executor.calls == ["report"], "s1 already completed — only s2 should run"
    assert llm.call_count == 0, "resume must not trigger a fresh planning LLM call"


@pytest.mark.asyncio
async def test_dangling_step_started_reruns(tmp_path):
    events, checkpoints = _stores(tmp_path)
    await events.append(
        Event.make(
            PlanEventType.PLAN_CREATED,
            "R3",
            version=1,
            steps=[
                {"step_id": "s1", "action": "collect", "depends_on": [], "status": "pending"},
            ],
        )
    )
    await events.append(Event.make(PlanEventType.STEP_STARTED, "R3", version=1, step_id="s1"))

    executor = _RecordingExecutor()
    planner = PlanExecutor(
        _plan_llm(_two_step_plan()),
        executor,
        system="sys",
        messages=[{"role": "user", "content": "resume"}],
        event_log=events,
        checkpoint_store=checkpoints,
    )

    async for _ in planner.stream("resume", run_id="R3"):
        pass

    assert executor.calls == ["collect"], "dangling step must re-run (tools must be idempotent)"


@pytest.mark.asyncio
async def test_checkpoint_only_at_step_boundary(tmp_path):
    events, checkpoints = _stores(tmp_path)
    executor = _RecordingExecutor()
    planner = PlanExecutor(
        _plan_llm({"steps": [{"id": "s1", "action": "collect", "params": {}, "depends_on": []}]}),
        executor,
        system="sys",
        messages=[{"role": "user", "content": "go"}],
        event_log=events,
        checkpoint_store=checkpoints,
    )

    async for _ in planner.stream("go", run_id="R4"):
        pass

    run = await checkpoints.load("R4")
    assert run is not None
    assert run.plan_state["steps"]["s1"]["status"] == "completed"
    completed = [
        e for e in await events.get_events("R4") if e.event_type == PlanEventType.STEP_COMPLETED
    ][0]
    assert run.plan_last_seq == completed.seq


@pytest.mark.asyncio
async def test_retry_same_run_id_after_plan_failure_does_not_conflict(tmp_path):
    events, checkpoints = _stores(tmp_path)
    executor = _RecordingExecutor()

    bad_llm = FakeLLMAdapter(
        responses=[LLMResponse(content="not json at all", stop_reason="end_turn")]
    )
    planner1 = PlanExecutor(
        bad_llm,
        executor,
        system="sys",
        messages=[{"role": "user", "content": "go"}],
        event_log=events,
        checkpoint_store=checkpoints,
    )
    streamed1: list = []
    async for event in planner1.stream("go", run_id="R5"):
        streamed1.append(event)
    run1 = _final_run(streamed1)
    assert run1.state.value == "failed"
    assert run1.last_error == "Failed to parse plan JSON — no steps to execute"

    planner2 = PlanExecutor(
        _plan_llm({"steps": [{"id": "s1", "action": "collect", "params": {}, "depends_on": []}]}),
        executor,
        system="sys",
        messages=[{"role": "user", "content": "go"}],
        event_log=events,
        checkpoint_store=checkpoints,
    )
    streamed2: list = []
    async for event in planner2.stream("go", run_id="R5"):
        streamed2.append(event)
    run2 = _final_run(streamed2)
    assert run2.state.value == "completed", (
        f"retry should succeed, got state={run2.state.value} (last_error={run2.last_error})"
    )
    assert executor.calls == ["collect"]


@pytest.mark.asyncio
async def test_complete_step_aborts_when_step_obsoleted_mid_flight(tmp_path):
    from prodagent.kernel.state import AgentRun
    from prodagent.kernel.types import RunState, ToolCall
    from prodagent.plan.dag import Plan, PlanStep, StepStatus

    events, checkpoints = _stores(tmp_path)
    planner = PlanExecutor(
        _plan_llm({"steps": []}),
        _RecordingExecutor(),
        event_log=events,
        checkpoint_store=checkpoints,
    )
    plan = Plan(plan_id="R5")
    s0 = PlanStep(step_id="s0", action="do_first", depends_on=[])
    step = PlanStep(step_id="s1", action="do_thing", depends_on=["s0"])
    plan.add_steps([s0, step])
    run = AgentRun(run_id="R5", task="t")
    run.state = RunState.RUNNING

    s0.status = StepStatus.FAILED
    step.status = StepStatus.RUNNING
    plan.mark_downstream_obsolete("s0")
    assert step.status is StepStatus.OBSOLETE

    call = ToolCall(name="do_thing", params={}, call_id="c1")
    prior_messages = len(run.messages)
    prior_tool_history = len(run.tool_history)

    await planner._step_runner._complete(
        step,
        result={"ok": True},
        call=call,
        plan=plan,
        run=run,
    )

    assert step.status is StepStatus.OBSOLETE, "OBSOLETE step was overwritten to COMPLETED"
    assert step.output_ref is None
    assert len(run.messages) == prior_messages, "tool result was appended despite abort"
    assert len(run.tool_history) == prior_tool_history
    evs = await events.get_events("R5")
    assert all(e.event_type != PlanEventType.STEP_COMPLETED for e in evs), (
        "StepCompleted event was emitted for an aborted step"
    )


@pytest.mark.asyncio
async def test_cold_start_replan_marks_replaced_step_obsolete(tmp_path):
    from prodagent.plan.dag import Plan, StepStatus
    from prodagent.plan.event_log import apply_event

    events, checkpoints = _stores(tmp_path)

    await events.append(
        Event.make(
            PlanEventType.PLAN_CREATED,
            "R6",
            version=1,
            steps=[
                {"step_id": "s1", "action": "collect", "depends_on": [], "status": "pending"},
                {"step_id": "s2", "action": "report", "depends_on": ["s1"], "status": "pending"},
            ],
        )
    )
    await events.append(Event.make(PlanEventType.STEP_STARTED, "R6", version=1, step_id="s1"))
    await events.append(
        Event.make(
            PlanEventType.STEP_COMPLETED, "R6", version=1, step_id="s1", output_ref={"ok": 1}
        )
    )
    await events.append(Event.make(PlanEventType.STEP_STARTED, "R6", version=1, step_id="s2"))
    await events.append(
        Event.make(PlanEventType.STEP_FAILED, "R6", version=1, step_id="s2", error="boom")
    )
    await events.append(
        Event.make(
            PlanEventType.PLAN_REPLANNED,
            "R6",
            version=2,
            new_steps=[
                {
                    "step_id": "s2b",
                    "action": "report_v2",
                    "depends_on": ["s1"],
                    "status": "pending",
                    "replaces_step_id": "s2",
                }
            ],
        )
    )

    state: dict[str, Any] = {"steps": {}, "version": 0}
    for event in await events.get_events("R6"):
        apply_event(state, event)

    plan = Plan.from_state(state, plan_id="R6")
    assert plan.version == 2
    assert plan.get_step("s2").status is StepStatus.OBSOLETE
    assert plan.get_step("s2b").status is StepStatus.PENDING
    assert plan.get_step("s2b").replaces_step_id == "s2"


@pytest.mark.asyncio
async def test_resume_rebases_checkpoint_version(tmp_path):
    events, checkpoints = _stores(tmp_path)
    executor = _RecordingExecutor()
    planner = PlanExecutor(
        _plan_llm(_two_step_plan()),
        executor,
        system="sys",
        messages=[{"role": "user", "content": "do"}],
        event_log=events,
        checkpoint_store=checkpoints,
    )
    async for _ in planner.stream("do", run_id="R7"):
        pass

    stored = await checkpoints.load("R7")
    assert stored is not None
    assert stored.checkpoint_version >= 1

    executor2 = _RecordingExecutor()
    planner2 = PlanExecutor(
        _plan_llm(_two_step_plan()),
        executor2,
        system="sys",
        messages=[{"role": "user", "content": "resume"}],
        event_log=events,
        checkpoint_store=checkpoints,
    )
    streamed2: list = []
    async for event in planner2.stream("resume", run_id="R7"):
        streamed2.append(event)

    assert executor2.calls == [], "both steps already completed — nothing to re-run"
    run2 = _final_run(streamed2)
    assert run2.checkpoint_version == stored.checkpoint_version
