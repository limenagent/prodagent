"""Finalize — pure functions for settling a finished plan run.

No IO, no side effects: given the run and plan as they ended, decide the
terminal stream event and where ``final_output`` comes from. Pure on
purpose — settling logic that can't trigger anything can be tested alone
and can't produce "the ending caused another bug" surprises. The mirror
half of ``bootstrap.py``: opening answers "where did this start", this
answers "how does it end"."""

from __future__ import annotations

from typing import TYPE_CHECKING

from prodagent.kernel.types import (
    RunCompletedEvent,
    RunFailedEvent,
    RunState,
    RunSuspendedEvent,
    StepStatus,
)
from prodagent.plan.step_runner import format_step_output

if TYPE_CHECKING:
    from prodagent.kernel.state import AgentRun
    from prodagent.kernel.types import AgentEvent
    from prodagent.plan.dag import Plan, PlanStep


def terminal_event(run: AgentRun) -> AgentEvent:
    """The one event every stream consumer ends on — suspension and failure
    included. A stream that just stops leaves consumers guessing; this
    guarantees they never have to."""
    if run.state is RunState.SUSPENDED:
        return RunSuspendedEvent(run=run)
    if run.state is RunState.FAILED:
        return RunFailedEvent(run=run, error=run.last_error or "")
    return RunCompletedEvent(run=run)


def finalize_run(run: AgentRun, plan: Plan | None) -> None:
    """Settle the run's final output. Priority: the plan's designated
    terminal step (``is_terminal=True``) speaks for the plan; without one,
    the last step to complete does — in a DAG, output flows downhill, so
    the sink is the answer. A pending handoff keeps the handoff message:
    the peer continues the story, this run's output is not the ending."""
    if run.state is RunState.RUNNING:
        run.complete()
    if run.pending_handoff is not None:
        return
    if plan is None:
        return

    terminal = next(
        (s.output_ref for s in plan.steps if s.is_terminal and s.status is StepStatus.COMPLETED),
        None,
    )
    if terminal is not None:
        run.final_output = format_step_output(terminal)
        return

    sink = select_terminal_step(plan)
    if sink is not None:
        run.final_output = format_step_output(sink.output_ref)


def select_terminal_step(plan: Plan) -> PlanStep | None:
    """No explicit terminal → the last step to complete is the answer: output
    flows downhill in a DAG, so the sink speaks for the plan."""
    completed = [s for s in plan.steps if s.status is StepStatus.COMPLETED]
    if not completed:
        return None
    timed = [s for s in completed if s.completed_at > 0]
    if timed:
        return max(timed, key=lambda s: s.completed_at)
    return completed[-1]
