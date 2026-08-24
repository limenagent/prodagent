"""PLAN_FIRST step failures must carry the tool error's hint.

Resource-contention errors are only useful if the replanning LLM can read the
hint and yield to another task (chapter 10) — StepFailed must not drop it.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from prodagent.kernel.state import AgentRun
from prodagent.plan.dag import Plan, PlanStep
from prodagent.plan.step_runner import StepFailed, StepRunner

if TYPE_CHECKING:
    from prodagent.kernel.types import ToolCall

_BUSY_RAW = {
    "error": True,
    "reason": "resource_busy",
    "error_severity": "yellow",
    "message": "Resource 'progress_file' is busy (held by another agent).",
    "hint": "Try an alternative task or retry later.",
}


class _StubEventLog:
    async def record_step_started(self, plan: Plan, run: AgentRun, step_id: str) -> int:
        return 0


async def _busy_executor(call: ToolCall) -> dict:
    return _BUSY_RAW


@pytest.mark.asyncio
async def test_resource_busy_failure_message_includes_hint_for_replan():
    plan = Plan(plan_id="p-hint")
    step = PlanStep(step_id="s1", action="write_progress")
    plan.add_steps([step])
    run = AgentRun(run_id="r-hint", task="t")
    runner = StepRunner(_busy_executor, _StubEventLog(), agent_name="test")

    outcome = await runner.run_one(step, plan, run)

    assert isinstance(outcome, StepFailed)
    msg = str(outcome.error)
    assert "Resource 'progress_file' is busy" in msg
    assert "hint: Try an alternative task or retry later." in msg


@pytest.mark.asyncio
async def test_failure_message_without_hint_stays_clean():
    raw = {"error": True, "reason": "format_error", "message": "bad payload"}

    async def executor(call: ToolCall) -> dict:
        return raw

    plan = Plan(plan_id="p-plain")
    step = PlanStep(step_id="s1", action="upload")
    plan.add_steps([step])
    run = AgentRun(run_id="r-plain", task="t")
    runner = StepRunner(executor, _StubEventLog(), agent_name="test")

    outcome = await runner.run_one(step, plan, run)

    assert isinstance(outcome, StepFailed)
    assert str(outcome.error) == "bad payload"
