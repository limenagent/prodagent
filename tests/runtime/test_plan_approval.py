from __future__ import annotations

import json
from typing import Any

import pytest

from prodagent.backends.file.checkpoint import FileCheckpointStore
from prodagent.backends.file.event_log import FileEventLog
from prodagent.core.exceptions import SuspendPendingApproval
from prodagent.hooks import HookRegistry
from prodagent.kernel.bus import BlockingResult, Gate
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


def _stores(tmp_path):
    return (
        FileEventLog(tmp_path / "events"),
        FileCheckpointStore(directory=tmp_path / "checkpoints"),
    )


def _basic_plan() -> dict:
    return {
        "steps": [
            {"id": "s1", "action": "noop", "params": {}, "depends_on": []},
        ]
    }


async def _noop_executor(call, *, run_id: str = "") -> dict:
    return {"status": "ok", "action": call.name}


async def _reject_plan(*_, **__) -> BlockingResult:
    return BlockingResult(blocked=True, reason="reviewer said no")


async def _approve_plan(*_, **__) -> BlockingResult:
    return BlockingResult(blocked=False)


async def _suspend_plan(*, plan_id, **__) -> BlockingResult:
    raise SuspendPendingApproval(
        f"plan {plan_id} suspended pending review", tool="plan", request_id="req-1"
    )


def _executor_with_checker(checker, tmp_path):
    events, checkpoints = _stores(tmp_path)
    hooks = HookRegistry()
    hooks.register_checker(Gate.PLAN_APPROVAL, checker, priority=100)
    return PlanExecutor(
        _plan_llm(_basic_plan()),
        _noop_executor,
        system="sys",
        messages=[{"role": "user", "content": "do"}],
        hooks=hooks,
        event_log=events,
        checkpoint_store=checkpoints,
    )


@pytest.mark.asyncio
async def test_plan_rejected_by_checkpoint_fails_run(tmp_path):
    planner = _executor_with_checker(_reject_plan, tmp_path)

    streamed: list = []
    async for event in planner.stream("do", run_id="R1"):
        streamed.append(event)

    run = _final_run(streamed)
    assert run.state.value == "failed"
    assert "reviewer said no" in (run.last_error or "")
    from prodagent.kernel.events import StepStartedEvent

    assert not any(isinstance(e, StepStartedEvent) for e in streamed)


@pytest.mark.asyncio
async def test_plan_approved_proceeds_to_execution(tmp_path):
    planner = _executor_with_checker(_approve_plan, tmp_path)

    streamed: list = []
    async for event in planner.stream("do", run_id="R2"):
        streamed.append(event)

    run = _final_run(streamed)
    assert run.state.value == "completed"
    from prodagent.kernel.events import StepCompletedEvent

    assert any(isinstance(e, StepCompletedEvent) for e in streamed)


@pytest.mark.asyncio
async def test_plan_suspend_pends_approval_id(tmp_path):
    planner = _executor_with_checker(_suspend_plan, tmp_path)

    streamed: list = []
    async for event in planner.stream("do", run_id="R3"):
        streamed.append(event)

    run = _final_run(streamed)
    assert run.state.value == "suspended"
    assert run.pending_approval_id == "req-1"
    from prodagent.kernel.events import StepStartedEvent

    assert not any(isinstance(e, StepStartedEvent) for e in streamed)


@pytest.mark.asyncio
async def test_plan_suspend_resume_via_pending_approval_id(tmp_path):
    events, checkpoints = _stores(tmp_path)
    hooks = HookRegistry()
    decisions: dict[str, str] = {}

    async def checker(*, plan_id, pending_approval_id=None, **__):
        if pending_approval_id is not None and pending_approval_id in decisions:
            if decisions[pending_approval_id] == "reject":
                return BlockingResult(blocked=True, reason="deferred reject")
            return BlockingResult(blocked=False)
        raise SuspendPendingApproval(f"plan {plan_id} suspended", tool="plan", request_id="req-1")

    hooks.register_checker(Gate.PLAN_APPROVAL, checker, priority=100)
    planner = PlanExecutor(
        _plan_llm(_basic_plan()),
        _noop_executor,
        system="sys",
        messages=[{"role": "user", "content": "do"}],
        hooks=hooks,
        event_log=events,
        checkpoint_store=checkpoints,
    )

    streamed: list = []
    async for event in planner.stream("do", run_id="R4"):
        streamed.append(event)
    run = _final_run(streamed)
    assert run.state.value == "suspended"
    assert run.pending_approval_id == "req-1"

    decisions["req-1"] = "approve"

    streamed2: list = []
    async for event in planner.stream("do", run_id="R4"):
        streamed2.append(event)
    run2 = _final_run(streamed2)
    assert run2.state.value == "completed"
    from prodagent.kernel.events import StepCompletedEvent

    assert any(isinstance(e, StepCompletedEvent) for e in streamed2)


@pytest.mark.asyncio
async def test_no_hooks_no_plan_approval_gate(tmp_path):
    events, checkpoints = _stores(tmp_path)
    planner = PlanExecutor(
        _plan_llm(_basic_plan()),
        _noop_executor,
        system="sys",
        messages=[{"role": "user", "content": "do"}],
        hooks=None,
        event_log=events,
        checkpoint_store=checkpoints,
    )

    streamed: list = []
    async for event in planner.stream("do", run_id="R5"):
        streamed.append(event)
    run = _final_run(streamed)
    assert run.state.value == "completed"
