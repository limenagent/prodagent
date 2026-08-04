from __future__ import annotations

from typing import Any

from prodagent.core.state.run import AgentRun
from prodagent.core.types import RunState
from prodagent.runtime.executors.plan_first import PlanExecutor
from prodagent.runtime.plan.dag import Plan, PlanStep, StepStatus


class _FakePlanner:
    def __init__(self, plan: Plan) -> None:
        self._plan = plan

    async def generate(self, *args: Any, **kwargs: Any) -> Plan | None:
        return self._plan

    async def replan(self, *args: Any, **kwargs: Any) -> list[PlanStep]:
        return []


class _StubExecutor(PlanExecutor):
    pass


def _make_run() -> AgentRun:
    return AgentRun(run_id="test-run", task="test")


def _make_step(
    step_id: str,
    *,
    action: str = "noop",
    is_terminal: bool = False,
    status: StepStatus = StepStatus.COMPLETED,
    output_ref: Any = None,
) -> PlanStep:
    s = PlanStep(step_id=step_id, action=action, is_terminal=is_terminal)
    s.status = status
    s.output_ref = output_ref
    return s


class TestFinalizeRunTerminalStep:
    def test_terminal_step_output_becomes_final_output(self):
        run = _make_run()
        plan = Plan(plan_id="test-run")
        plan._steps = {
            "investigate": _make_step(
                "investigate",
                output_ref="Diagnosis: bad config map caused the crash",
            ),
            "fix": _make_step(
                "fix",
                action="restart_pod",
                is_terminal=True,
                output_ref={"restarted": True},
            ),
        }
        PlanExecutor._finalize_run(run, plan)

        assert run.state is RunState.COMPLETED
        assert run.final_output == str({"restarted": True})

    def test_non_terminal_last_step_does_not_override_terminal(self):
        run = _make_run()
        plan = Plan(plan_id="test-run")
        plan._steps = {
            "report": _make_step(
                "report",
                is_terminal=True,
                output_ref="Incident report: root cause was X",
            ),
            "notify": _make_step(
                "notify",
                action="send_notification",
                output_ref={"sent": True},
            ),
        }
        PlanExecutor._finalize_run(run, plan)

        assert run.final_output == "Incident report: root cause was X"
        assert run.final_output != str({"sent": True})

    def test_no_terminal_step_falls_back_to_last_tool_result(self):
        run = _make_run()
        plan = Plan(plan_id="test-run")
        plan._steps = {
            "step_a": _make_step("step_a", output_ref={"a": 1}),
            "step_b": _make_step("step_b", output_ref={"b": 2}),
        }
        PlanExecutor._finalize_run(run, plan)

        assert run.final_output == str({"b": 2})

    def test_terminal_step_not_completed_falls_back(self):
        run = _make_run()
        plan = Plan(plan_id="test-run")
        plan._steps = {
            "step_a": _make_step("step_a", output_ref={"a": 1}),
            "terminal": _make_step(
                "terminal",
                is_terminal=True,
                status=StepStatus.FAILED,
                output_ref=None,
            ),
        }
        PlanExecutor._finalize_run(run, plan)

        assert run.final_output == str({"a": 1})

    def test_suspended_run_not_clobbered_to_completed(self):
        run = _make_run()
        run.state = RunState.SUSPENDED
        plan = Plan(plan_id="test-run")
        plan._steps = {
            "terminal": _make_step("terminal", is_terminal=True, output_ref="result"),
        }
        PlanExecutor._finalize_run(run, plan)

        assert run.state is RunState.SUSPENDED
        assert run.final_output == "result"

    def test_terminal_spawn_agent_step_unwraps_child_output(self):
        run = _make_run()
        plan = Plan(plan_id="test-run")
        child_result = {
            "agent": "sar_submitter",
            "state": "completed",
            "output": "✅ SAR 报告已成功提交至监管系统。",
            "turns": 2,
            "cost_usd": 0.0,
            "input_tokens": 0,
            "output_tokens": 0,
            "tool_history": [],
        }
        plan._steps = {
            "submit_sar": _make_step(
                "submit_sar",
                action="spawn_agent",
                is_terminal=True,
                output_ref=child_result,
            ),
        }
        PlanExecutor._finalize_run(run, plan)

        assert run.final_output == "✅ SAR 报告已成功提交至监管系统。"
        assert "agent" not in run.final_output
        assert "state" not in run.final_output


class TestPlanStepTerminalSerialization:
    def test_to_dict_includes_is_terminal(self):
        step = PlanStep(step_id="s1", action="noop", is_terminal=True)
        d = step.to_dict()
        assert d["is_terminal"] is True

    def test_from_state_restores_is_terminal(self):
        plan = Plan(plan_id="p1")
        plan._steps = {
            "s1": PlanStep(step_id="s1", action="noop", is_terminal=True),
            "s2": PlanStep(step_id="s2", action="noop", is_terminal=False),
        }
        state = plan.to_state()

        restored = Plan.from_state(state, plan_id="p1")
        assert restored._steps["s1"].is_terminal is True
        assert restored._steps["s2"].is_terminal is False

    def test_from_state_defaults_is_terminal_false_for_legacy_state(self):
        legacy_state = {
            "version": 1,
            "steps": {
                "s1": {
                    "step_id": "s1",
                    "action": "noop",
                    "params": {},
                    "depends_on": [],
                }
            },
        }
        plan = Plan.from_state(legacy_state, plan_id="p1")
        assert plan._steps["s1"].is_terminal is False
