"""Handoff as a graph fact — the chain lives inside ONE run.

The laws: a Handoff command instantiates the named peer as a fresh
terminal node in the SAME plan (Send-style, root-ready next wave); the
sender completes normally; the run never ends mid-chain — the wave loop
keeps going until the peer's answer lands, and that answer (the latest
completed terminal) is the run's final output. A wave whose ready nodes
all go un-dispatched is a stall, not a spin.
"""

from __future__ import annotations

import pytest

from prodagent.base.errors import Stalled
from prodagent.kernel.bodies import FnBody, ToolBody
from prodagent.kernel.command import Handoff
from prodagent.kernel.graph import Node, Plan
from prodagent.kernel.run import Run
from prodagent.kernel.scheduler import Scheduler
from prodagent.kernel.types import RunState
from prodagent.tooling.dispatcher import ToolDispatcher


def _scheduler(fns: dict, plan: Plan, **kwargs) -> Scheduler:
    return Scheduler(
        initial_plan=plan,
        dispatcher=ToolDispatcher({}),
        fns=fns,
        **kwargs,
    )


async def _drive(scheduler: Scheduler):
    events = []
    async for event in scheduler.stream("task"):
        events.append(event)
    return events


def _chain_plan() -> Plan:
    return Plan(nodes=[Node(node_id="entry", body=FnBody(fn="entry"))])


def _chain_fns() -> dict:
    def entry() -> Handoff:
        return Handoff(peer="remediator", task="fix it")

    def peer_body(task: str = "") -> str:
        return f"fixed: {task}"

    return {"entry": entry, "peer:remediator": peer_body}


async def test_handoff_grows_the_plan_and_carries_the_chain():
    scheduler = _scheduler(
        _chain_fns(),
        _chain_plan(),
        resolve_peer=lambda name: FnBody(fn=f"peer:{name}"),
    )
    events = await _drive(scheduler)
    run = events[-1].run

    assert run.state is RunState.COMPLETED
    # the executor derived its own plan copy; the RUN's node states carry
    # the truth: a fresh terminal peer node executed in this same run
    peer_states = [s for nid, s in run.node_states.items() if nid.startswith("peer:remediator#")]
    assert len(peer_states) == 1, "the peer joined the plan as a fresh node"
    assert peer_states[0].status.value == "completed"
    started = [e.node_id for e in events if type(e).__name__ == "NodeStartedEvent"]
    assert started == ["entry", "peer:remediator#1"], "the chain ran inside one stream"
    # the terminal answer is the peer's (wire-wrapped: a fn node's output
    # rides the tool-result envelope)
    assert run.final_output is not None and "fixed: fix it" in run.final_output


async def test_handoff_without_a_resolver_is_a_loud_composition_bug():
    scheduler = _scheduler(_chain_fns(), _chain_plan())
    with pytest.raises(ValueError, match="no peer resolver"):
        await _drive(scheduler)


async def test_unknown_peer_is_named_loudly():
    def entry() -> Handoff:
        return Handoff(peer="ghost", task="boo")

    scheduler = _scheduler(
        {"entry": entry},
        _chain_plan(),
        resolve_peer=lambda name: None,
    )
    with pytest.raises(ValueError, match="ghost"):
        await _drive(scheduler)


async def test_a_wave_that_dispatches_nothing_stalls_immediately():
    """The empty-wave invariant: nothing external arrives between waves, so
    a wave whose ready nodes all went un-dispatched would repeat forever —
    it stalls at once, naming the blocker, instead of burning the cap."""
    plan = Plan(nodes=[Node(node_id="unit", body=ToolBody(tool="never"))])
    scheduler = _scheduler({}, plan)
    run = Run(run_id="r", task="t")
    run.fail("not running anymore")  # the dispatch loop breaks on this
    with pytest.raises(Stalled, match="not running"):
        async for _ in scheduler._waves(plan, run):
            pass
