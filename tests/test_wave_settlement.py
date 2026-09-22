"""Wave-settlement laws: the wave is a consistency boundary for failure too,
delegation never waits on itself, a Goto input survives a park, and terminal
states are durable."""

import asyncio

import pytest

from src.kernel import (
    FnBody,
    Node,
    Outcome,
    Plan,
    Run,
    RunState,
    Scheduler,
    last,
)
from src.kernel.eventlog import (
    INTERRUPTED,
    NODE_COMPLETED,
    NODE_FAILED,
    RUN_FAILED,
    STATE_DELTA,
)


async def test_deep_delegation_never_waits_on_itself():
    """One slot pool per Run, not per scheduler: with concurrency=1 a chain of
    nested sub-Runs must still finish (a scheduler-global semaphore deadlocks
    here — each parent would hold the only slot while waiting for its child)."""

    def leaf(_x, _ctx):
        return Outcome.ok("done")

    def chain(depth):
        if depth == 0:
            return Plan().add(Node("leaf", FnBody(leaf), terminal=True))

        async def spawn_child(_x, ctx):
            result = await ctx.spawn(chain(depth - 1), "go deeper")
            return Outcome.ok(result["output"])

        return Plan().add(Node(f"n{depth}", FnBody(spawn_child), terminal=True))

    sch = Scheduler(concurrency=1)
    run = await asyncio.wait_for(sch.run(chain(3)), timeout=5)
    assert run.state == RunState.COMPLETED
    assert run.final_output == "done"


async def test_a_failed_wave_still_settles_its_siblings():
    """Fail-fast stops the Run, not the settlement: a sibling that succeeded
    keeps its fold and its NODE_COMPLETED; the failed node gets NODE_FAILED."""

    async def boom(_x, _ctx):
        raise RuntimeError("boom")

    async def ok(_x, _ctx):
        return Outcome.ok(kept="yes")

    sch = Scheduler()
    plan = Plan(channels={"kept": last(None)}).add(
        Node("boom", FnBody(boom), terminal=True),
        Node("ok", FnBody(ok), terminal=True),
    )
    run = await sch.run(plan)
    assert run.state == RunState.FAILED
    assert run.shared["kept"] == "yes"  # the sibling's delta was folded
    kinds = [e.kind for e in await sch.eventlog.events(run.run_id)]
    assert NODE_COMPLETED in kinds
    assert STATE_DELTA in kinds
    assert kinds.index(NODE_FAILED) < kinds.index(RUN_FAILED)


async def test_failure_wins_over_a_same_wave_park_but_the_ask_is_logged():
    """When one node parks and a sibling fails in the same wave, the Run fails —
    but the parked question is still a fact the stream must carry."""

    def ask(_x, ctx):
        if ctx.resume_value is None:
            return Outcome.park("approval", question="approve?")
        return Outcome.ok(ctx.resume_value)

    async def boom(_x, _ctx):
        raise RuntimeError("boom")

    sch = Scheduler()
    plan = Plan().add(
        Node("ask", FnBody(ask), terminal=True), Node("boom", FnBody(boom), terminal=True)
    )
    run = await sch.run(plan)
    assert run.state == RunState.FAILED

    events = await sch.eventlog.events(run.run_id)
    kinds = [e.kind for e in events]
    assert kinds.index(INTERRUPTED) < kinds.index(RUN_FAILED)
    parked = next(e for e in events if e.kind == INTERRUPTED).data["parked"]
    assert parked["ask"]["question"] == "approve?"
    assert (await sch.store.load(run.run_id))["state"] == "failed"
    with pytest.raises(RuntimeError, match="only a suspended run can resume"):
        await sch.resume(plan, run.run_id, "y")  # history, not something to resume


async def test_a_goto_payload_survives_a_park():
    """The input a Goto carries is consumed by the node's terminal state, not by
    reading it: a parked node re-runs with the same input after resume."""

    inputs: list = []

    async def send_over(_x, _ctx):
        return Outcome.goto("worker", "hand-off brief")

    def worker(x, ctx):
        inputs.append(x)
        if ctx.resume_value is None:
            return Outcome.park("input", question="more info?")
        return Outcome.ok(x)

    def build():
        p = Plan()
        p.add(Node("start", FnBody(send_over)), Node("worker", FnBody(worker), terminal=True))
        p.edge("start", "worker")
        return p

    sch = Scheduler()
    run = await sch.run(build())
    assert run.state == RunState.SUSPENDED
    assert inputs == ["hand-off brief"]

    resumed = await sch.resume(build(), run.run_id, "answer")
    assert resumed.state == RunState.COMPLETED
    assert inputs == ["hand-off brief", "hand-off brief"]


async def test_terminal_states_are_durable():
    """Even the cheap durability mode records the outcome: a Run found in a
    store is never left claiming RUNNING after it finished."""

    def ok(_x, _ctx):
        return "fine"

    async def boom(_x, _ctx):
        raise RuntimeError("boom")

    for body, expect in ((FnBody(ok), "completed"), (FnBody(boom), "failed")):
        sch = Scheduler(durability="exit")
        run = await sch.run(Plan().add(Node("n", body, terminal=True)))
        assert str(run.state) == expect
        assert (await sch.store.load(run.run_id))["state"] == expect


async def test_external_cancellation_propagates_not_fails():
    """External cancellation is not a node failure: it propagates unchanged and
    the Run is simply not finished (no FAILED), so the caller decides."""

    async def slow(_x, _ctx):
        await asyncio.sleep(5)

    sch = Scheduler()
    plan = Plan().add(Node("n", FnBody(slow), terminal=True))
    run = Run.start(plan)
    task = asyncio.create_task(sch.drive(plan, run))
    await asyncio.sleep(0.05)  # let the node start
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert run.state == RunState.RUNNING
