from __future__ import annotations

import json

import pytest

from prodagent.backends.file.checkpoint import FileCheckpointStore
from prodagent.backends.file.event_log import FileEventLog
from prodagent.core.exceptions import BudgetExceeded
from prodagent.kernel.budget import HardBudget
from prodagent.kernel.events import RunCompletedEvent, RunFailedEvent, RunSuspendedEvent
from prodagent.kernel.types import LLMResponse
from prodagent.llm.fake import FakeLLMAdapter
from prodagent.plan.executor import PlanExecutor

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


def _multi_step_plan(n: int) -> dict:
    return {
        "steps": [
            {"id": f"s{i}", "action": f"step_{i}", "params": {}, "depends_on": []} for i in range(n)
        ]
    }


def _stores(tmp_path):
    return (
        FileEventLog(tmp_path / "events"),
        FileCheckpointStore(directory=tmp_path / "checkpoints"),
    )


class _CountingExecutor:
    def __init__(self) -> None:
        self.calls: list[str] = []

    async def __call__(self, call) -> dict:
        self.calls.append(call.name)
        return {"status": "ok", "action": call.name}


@pytest.mark.asyncio
async def test_plan_first_budget_turns_trips_mid_plan(tmp_path):
    events, checkpoints = _stores(tmp_path)
    executor = _CountingExecutor()
    planner = PlanExecutor(
        _plan_llm(_multi_step_plan(3)),
        executor,
        system="sys",
        messages=[{"role": "user", "content": "do"}],
        event_log=events,
        checkpoint_store=checkpoints,
        budget=HardBudget(max_turns=1, max_cost_usd=100, max_tokens=1_000_000, max_seconds=600),
    )

    with pytest.raises(BudgetExceeded) as exc_info:
        async for _ in planner.stream("do", run_id="R1"):
            pass

    assert exc_info.value.context.get("axis") == "turns"
    assert len(executor.calls) < 3, "budget should have halted before all steps ran"


@pytest.mark.asyncio
async def test_plan_first_budget_zero_turns_blocks_even_plan_generation(tmp_path):
    events, checkpoints = _stores(tmp_path)
    planner = PlanExecutor(
        _plan_llm(_multi_step_plan(2)),
        _CountingExecutor(),
        system="sys",
        messages=[{"role": "user", "content": "do"}],
        event_log=events,
        checkpoint_store=checkpoints,
        budget=HardBudget(max_turns=0, max_cost_usd=100, max_tokens=1_000_000, max_seconds=600),
    )

    with pytest.raises(BudgetExceeded) as exc_info:
        async for _ in planner.stream("do", run_id="R2"):
            pass

    assert exc_info.value.context.get("axis") == "turns"


@pytest.mark.asyncio
async def test_plan_first_trips_on_sibling_spend_it_never_directly_incurred(tmp_path):
    from prodagent.kernel.budget import BudgetLedger

    events, checkpoints = _stores(tmp_path)
    budget = HardBudget(max_turns=50, max_cost_usd=0.9, max_tokens=1_000_000, max_seconds=600)
    ledger = BudgetLedger(max=budget)
    await ledger.commit(member="sibling", turns=0, tokens=0, cost_usd=0.95)
    planner = PlanExecutor(
        _plan_llm(_multi_step_plan(3)),
        _CountingExecutor(),
        system="sys",
        messages=[{"role": "user", "content": "do"}],
        event_log=events,
        checkpoint_store=checkpoints,
        budget=budget,
        budget_ledger=ledger,
    )

    with pytest.raises(BudgetExceeded) as exc_info:
        async for _ in planner.stream("do", run_id="R-sibling-spend"):
            pass

    assert exc_info.value.context.get("axis") == "cost_usd"


@pytest.mark.asyncio
async def test_plan_first_no_budget_runs_to_completion(tmp_path):
    events, checkpoints = _stores(tmp_path)
    executor = _CountingExecutor()
    planner = PlanExecutor(
        _plan_llm(_multi_step_plan(2)),
        executor,
        system="sys",
        messages=[{"role": "user", "content": "do"}],
        event_log=events,
        checkpoint_store=checkpoints,
        budget=None,
    )

    streamed: list = []
    async for event in planner.stream("do", run_id="R3"):
        streamed.append(event)

    run = _final_run(streamed)
    assert run.state.value == "completed"
    assert len(executor.calls) == 2
