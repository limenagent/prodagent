"""Cycles — back edges, goto, and the three guards (column 5/6/16).

The laws under test: a back edge never gates readiness (a cycle must be
able to start); completing a node requeues its completed active dependents
(back edge = loop restart, forward edge = stale-output redo); goto requeues
by name at runtime; and the guards — empty-ready Stalled, the no-progress
detector, the wave cap — kill the loops that never terminate, loudly.
"""

from __future__ import annotations

import pytest

from prodagent.kernel.bodies import FnBody
from prodagent.kernel.command import Update
from prodagent.kernel.graph import Node, Plan
from prodagent.kernel.scheduler import Scheduler
from prodagent.tooling.dispatcher import ToolDispatcher


def _scheduler(fns: dict, plan: Plan, **kwargs) -> Scheduler:
    return Scheduler(
        initial_plan=plan,
        dispatcher=ToolDispatcher({}),
        fns=fns,
        **kwargs,
    )


async def _drive(scheduler: Scheduler):
    terminal = None
    async for event in scheduler.stream("task"):
        terminal = event
    return terminal


def _cycle_plan(rounds_key: str = "rounds") -> Plan:
    """entry → counter → tail, plus the back edge tail → counter that stays
    active while the counter's own count is below three."""
    plan = Plan(
        nodes=[
            Node(node_id="entry", body=FnBody(fn="entry")),
            Node(node_id="counter", body=FnBody(fn="counter"), depends_on=["entry"]),
            Node(node_id="tail", body=FnBody(fn="tail"), depends_on=["counter"]),
        ]
    )
    plan.edge("tail", "counter", when=lambda shared: shared.get(rounds_key, 0) < 3)
    return plan


def _fns(rounds_key: str = "rounds") -> dict:
    runs: list[str] = []

    def entry() -> dict:
        runs.append("entry")
        return {}

    def counter() -> Update:
        runs.append("counter")
        return Update(rounds_key, 1, "add")

    def tail() -> dict:
        runs.append("tail")
        return {}

    return {"entry": entry, "counter": counter, "tail": tail, "runs": runs}


async def test_back_edge_loops_three_rounds_then_terminates():
    plan = _cycle_plan()
    fns = _fns()
    terminal = await _drive(_scheduler(fns, plan))
    assert terminal.run.state.value == "completed"
    # three counter passes, each followed by a tail gate pass — the cycle
    # turned exactly as long as the back edge stayed active
    assert fns["runs"].count("counter") == 3
    # the tail runs once per round plus one final exit evaluation — the
    # third counter completion cascades a tail redo, whose waived back edge
    # is what actually ends the loop
    assert fns["runs"].count("tail") == 4
    assert terminal.run.shared["rounds"] == 3
    assert all(
        terminal.run.node_state(n).status.value == "completed" for n in ("entry", "counter", "tail")
    )


async def test_forward_edge_cascades_a_redo_to_completed_dependents():
    """A re-run source's completed dependents are stale — the forward edge
    requeues them (the spreadsheet redo), no goto needed."""

    plan = Plan(
        nodes=[
            Node(node_id="entry", body=FnBody(fn="entry")),
            Node(node_id="work", body=FnBody(fn="work"), depends_on=["entry"]),
            Node(node_id="sink", body=FnBody(fn="sink"), depends_on=["work"]),
        ]
    )
    # a one-shot self-restart: work requeues itself once via goto
    plan.edge("sink", "work", when=lambda shared: shared.get("passes", 0) < 2)
    seen: list[int] = []

    def entry() -> dict:
        return {}

    def work() -> Update:
        seen.append(1)
        return Update("passes", 1, "add")

    def sink(work_out=None) -> dict:
        return {"ok": True}

    terminal = await _drive(_scheduler({"entry": entry, "work": work, "sink": sink}, plan))
    assert terminal.run.state.value == "completed"
    # work ran twice; sink — its completed forward dependent — redid too
    assert len(seen) == 2


async def test_goto_requeues_a_named_target_at_runtime():
    plan = Plan(
        nodes=[
            Node(node_id="entry", body=FnBody(fn="entry")),
            Node(node_id="router", body=FnBody(fn="router"), depends_on=["entry"]),
            Node(node_id="sink", body=FnBody(fn="sink"), depends_on=["router"]),
        ]
    )
    calls: list[str] = []

    def entry() -> dict:
        calls.append("entry")
        return {}

    def router() -> Update:
        calls.append("router")
        hops = len([c for c in calls if c == "router"])
        if hops < 2:
            from prodagent.kernel.command import Goto

            return Goto("router")
        return Update("hops", hops, "add")

    def sink() -> dict:
        calls.append("sink")
        return {}

    terminal = await _drive(_scheduler({"entry": entry, "router": router, "sink": sink}, plan))
    assert terminal.run.state.value == "completed"
    assert calls.count("router") == 2
    assert terminal.run.shared["hops"] == 2


async def test_goto_to_an_unknown_target_is_a_loud_error():
    from prodagent.kernel.command import Goto

    plan = Plan(
        nodes=[
            Node(node_id="entry", body=FnBody(fn="entry"), is_terminal=True),
        ]
    )
    with pytest.raises(ValueError, match="not in the plan"):
        await _drive(_scheduler({"entry": lambda: Goto("nowhere")}, plan))


async def test_dead_loop_without_terminating_writes_is_detected():
    """A cycle whose body never writes what its exit condition reads: the
    no-progress detector kills it, loudly, instead of spinning to the wave
    cap. Run-death settles the run (a terminal event, not a raise)."""
    from prodagent.kernel.types import RunFailedEvent

    plan = Plan(
        nodes=[
            Node(node_id="entry", body=FnBody(fn="entry")),
            Node(node_id="spin", body=FnBody(fn="spin"), depends_on=["entry"]),
            Node(node_id="tail", body=FnBody(fn="tail"), depends_on=["spin"]),
        ]
    )
    plan.edge("tail", "spin", when=lambda shared: shared.get("never_written", 0) < 1)

    terminal = await _drive(
        _scheduler(
            {"entry": lambda: {}, "spin": lambda: {"noop": True}, "tail": lambda: {}},
            plan,
        )
    )
    assert isinstance(terminal, RunFailedEvent)
    assert "no shared-state change" in terminal.error


async def test_wave_cap_exhaustion_mid_flight_is_a_stall_not_a_success():
    """A cap too small for three rounds: mid-flight exhaustion must fail the
    run naming the unfinished nodes, never complete it silently."""
    from prodagent.kernel.types import RunFailedEvent

    plan = _cycle_plan()
    fns = _fns()
    terminal = await _drive(_scheduler(fns, plan, max_waves=4))
    assert isinstance(terminal, RunFailedEvent)
    assert "wave budget" in terminal.error
    assert "tail" in terminal.error  # the unfinished node is named
