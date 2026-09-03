"""Finalize — pure functions for settling a finished plan run.

No IO, no side effects: given the run and plan as they ended, decide the
terminal stream event and where ``final_output`` comes from. Pure on
purpose — settling logic that can't trigger anything can be tested alone
and can't produce "the ending caused another bug" surprises. The mirror
half of ``bootstrap.py``: opening answers "where did this start", this
answers "how does it end"."""

from __future__ import annotations

from typing import TYPE_CHECKING

from prodagent.kernel.node_runner import format_node_output
from prodagent.kernel.types import (
    NodeStatus,
    RunCompletedEvent,
    RunFailedEvent,
    RunState,
    RunSuspendedEvent,
)

if TYPE_CHECKING:
    from prodagent.kernel.graph import Node, Plan
    from prodagent.kernel.run import Run
    from prodagent.kernel.types import AgentEvent


def terminal_event(run: Run) -> AgentEvent:
    """The one event every stream consumer ends on — suspension and failure
    included. A stream that just stops leaves consumers guessing; this
    guarantees they never have to."""
    if run.state is RunState.SUSPENDED:
        return RunSuspendedEvent(run=run)
    if run.state is RunState.FAILED:
        return RunFailedEvent(run=run, error=run.last_error or "")
    return RunCompletedEvent(run=run)


def finalize_run(run: Run, plan: Plan | None) -> None:
    """Settle the run's final output. Priority: the plan's designated
    terminal node (``is_terminal=True``) speaks for the plan; without one,
    the last node to complete does — in a DAG, output flows downhill, so
    the sink is the answer. A pending handoff keeps the handoff message:
    the peer continues the story, this run's output is not the ending."""
    if run.state is RunState.RUNNING:
        run.complete()
    if run.pending_handoff is not None:
        return
    if plan is None:
        return

    states = run.node_states
    terminal = next(
        (
            states[n.node_id].output_ref
            for n in plan.nodes.values()
            if n.is_terminal
            and n.node_id in states
            and states[n.node_id].status is NodeStatus.COMPLETED
        ),
        None,
    )
    if terminal is not None:
        run.final_output = format_node_output(terminal)
        return

    sink = select_terminal_node(plan, run)
    if sink is not None:
        run.final_output = format_node_output(states[sink.node_id].output_ref)


def select_terminal_node(plan: Plan, run: Run) -> Node | None:
    """No explicit terminal → the last node to complete is the answer: output
    flows downhill in a DAG, so the sink speaks for the plan."""
    states = run.node_states
    completed = [
        n
        for n in plan.nodes.values()
        if n.node_id in states and states[n.node_id].status is NodeStatus.COMPLETED
    ]
    if not completed:
        return None
    timed = [n for n in completed if states[n.node_id].completed_at > 0]
    if timed:
        return max(timed, key=lambda n: states[n.node_id].completed_at)
    return completed[-1]
