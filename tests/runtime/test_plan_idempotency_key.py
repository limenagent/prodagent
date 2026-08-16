"""PLAN_FIRST steps must inject a crash-stable idempotency key.

A dangling step (crashed between StepStarted and StepCompleted) is re-run on
restore. The re-run must re-derive the SAME key its first attempt used, or the
external system would treat it as a new request and execute the side effect
twice. The anchor is step_id + attempt number, both of which roll back with the
checkpoint — see chapter 14's "不丢钱" defence line 2.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from prodagent import SideEffectLevel, ToolMeta
from prodagent.core.state.run import AgentRun
from prodagent.runtime.plan.dag import Plan, PlanStep
from prodagent.runtime.plan.step_runner import StepRunner
from prodagent.tooling import tool
from prodagent.tooling.dispatcher import ToolDispatcher

if TYPE_CHECKING:
    from prodagent.core.types import ToolCall


class _StubEventLog:
    async def record_step_started(self, plan: Plan, run: AgentRun, step_id: str) -> int:
        return 0

    async def record_step_completed(
        self, plan: Plan, run: AgentRun, step_id: str, result: object
    ) -> int:
        return 0


def _capturing_tool(captured: list[str]):
    @tool(
        name="refund_order",
        meta=ToolMeta(
            name="refund_order",
            side_effect_level=SideEffectLevel.MEDIUM,
            enforced_idempotent=True,
        ),
    )
    async def refund_order(order_id: str, idempotency_key: str = "") -> dict:
        captured.append(idempotency_key)
        return {"refunded": order_id}

    return refund_order


def _plan_with_step(action: str = "refund_order") -> tuple[Plan, PlanStep]:
    plan = Plan(plan_id="p-idem")
    step = PlanStep(step_id="s1", action=action, params={"order_id": "A1"})
    plan.add_steps([step])
    return plan, step


def _step_runner(fn) -> StepRunner:
    dispatcher = ToolDispatcher({fn.name: fn})
    return StepRunner(lambda call: _execute(fn, call), _StubEventLog(), dispatcher=dispatcher)


async def _execute(fn, call: ToolCall):
    return await fn(**call.params, run_id="r-idem")


@pytest.mark.asyncio
async def test_enforced_idempotent_step_gets_step_anchored_key():
    captured: list[str] = []
    runner = _step_runner(_capturing_tool(captured))
    plan, step = _plan_with_step()
    run = AgentRun(run_id="r-idem", task="t")

    await runner.run_one(step, plan, run)

    assert captured == ["r-idem:s1:a1"]


@pytest.mark.asyncio
async def test_reexecuted_dangling_step_rederives_same_key():
    """Crash between side-effect and StepCompleted: the restored re-run must
    hit the external system with the same key (duplicate suppressed)."""
    first_attempt: list[str] = []
    runner = _step_runner(_capturing_tool(first_attempt))
    plan, step = _plan_with_step()
    run = AgentRun(run_id="r-idem", task="t")
    await runner.run_one(step, plan, run)
    assert first_attempt == ["r-idem:s1:a1"]

    # What the checkpoint holds: the step had not started when it was saved —
    # attempts=0, status pending. The STEP_STARTED event replays to "running",
    # which from_state flips back to PENDING (rerun, not skip).
    checkpoint_state = {
        "version": 1,
        "steps": {
            "s1": {
                "step_id": "s1",
                "action": "refund_order",
                "params": {"order_id": "A1"},
                "depends_on": [],
                "status": "running",
                "attempts": 0,
            }
        },
    }
    restored_plan = Plan.from_state(checkpoint_state, plan_id="p-idem")
    restored_step = restored_plan.get_step("s1")
    assert restored_step is not None and restored_step.status.value == "pending"

    rerun_keys: list[str] = []
    rerun_runner = _step_runner(_capturing_tool(rerun_keys))
    await rerun_runner.run_one(restored_step, restored_plan, AgentRun(run_id="r-idem", task="t"))

    assert rerun_keys == first_attempt


@pytest.mark.asyncio
async def test_model_supplied_key_not_overwritten():
    captured: list[str] = []
    fn = _capturing_tool(captured)
    runner = _step_runner(fn)
    plan, step = _plan_with_step()
    step.params = {"order_id": "A1", "idempotency_key": "client-supplied"}
    run = AgentRun(run_id="r-idem", task="t")

    await runner.run_one(step, plan, run)

    assert captured == ["client-supplied"]


@pytest.mark.asyncio
async def test_non_enforced_step_gets_no_key():
    captured: list[str] = []

    @tool(name="plain_read", readonly=True)
    async def plain_read(idempotency_key: str = "") -> dict:
        captured.append(idempotency_key)
        return {"ok": True}

    runner = _step_runner(plain_read)
    plan, step = _plan_with_step(action="plain_read")
    step.params = {}
    run = AgentRun(run_id="r-idem", task="t")

    await runner.run_one(step, plan, run)

    assert captured == [""]
