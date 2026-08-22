from __future__ import annotations

from prodagent.plan.dag import Plan, PlanStep, StepStatus


def _make_plan_with_steps() -> Plan:
    plan = Plan()
    plan.add_steps(
        [
            PlanStep(step_id="s1", action="fetch"),
            PlanStep(step_id="s2", action="transform", depends_on=["s1"]),
            PlanStep(step_id="s3", action="load", depends_on=["s2"]),
        ]
    )
    return plan


def test_step_attempts_default_zero():
    step = PlanStep(step_id="s1")
    assert step.attempts == 0


def test_step_attempts_persisted_in_to_dict():
    step = PlanStep(step_id="s1", action="x")
    step.attempts = 3
    d = step.to_dict()
    assert d["attempts"] == 3


def test_step_attempts_restored_from_state():
    plan = _make_plan_with_steps()
    plan.get_step("s1").attempts = 2
    state = plan.to_state()

    restored = Plan.from_state(state, plan_id=plan.plan_id)
    assert restored.get_step("s1").attempts == 2


def test_step_attempts_incremented_by_start_step():

    plan = _make_plan_with_steps()
    step = plan.get_step("s1")

    step.status = StepStatus.RUNNING
    step.attempts += 1
    step.status = StepStatus.PENDING
    step.status = StepStatus.RUNNING
    step.attempts += 1

    assert step.attempts == 2
