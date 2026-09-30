"""Interrupt: a node requests to let go and pause — the facts land in the log; resume replays the stream, feeds back an external value, re-runs the parked node and continues."""

import pytest

from src.kernel import (
    FnBody,
    InMemoryEventLog,
    Node,
    Outcome,
    Plan,
    RunState,
    Scheduler,
)


def build_plan():
    p = Plan()

    def approve(_x, ctx):
        if ctx.resume_value is None:
            return Outcome.park("approval", question="批准吗？")
        return Outcome.ok(ctx.resume_value)

    p.add(Node("approve", FnBody(approve), terminal=True))
    return p


def build_two_approvals_plan():
    def ask(name):
        def _approve(_x, ctx):
            if ctx.resume_value is None:
                return Outcome.park("approval", question=f"{name} 批准吗？")
            return Outcome.ok(ctx.resume_value)

        return _approve

    return Plan().add(
        Node("a", FnBody(ask("a")), terminal=True),
        Node("b", FnBody(ask("b")), terminal=True),
    )


async def test_suspend_then_resume():
    sch = Scheduler()
    run = await sch.run(build_plan())
    assert run.state == RunState.SUSPENDED
    assert run.interrupts["approve"].question == "批准吗？"

    rid = run.run_id
    run2 = await sch.resume(build_plan(), rid, {"approved": True})
    assert run2.state == RunState.COMPLETED
    assert run2.final_output == {"approved": True}


async def test_resume_rejects_a_drifted_plan():
    """A stream belongs to its blueprint: replaying it against a Plan whose
    static node set differs fails loudly (a KeyError on the unknown node)
    instead of mis-wiring silently."""
    sch = Scheduler()
    run = await sch.run(build_plan())
    assert run.state == RunState.SUSPENDED

    drifted = Plan().add(Node("elsewhere", FnBody(lambda x, ctx: x), terminal=True))
    with pytest.raises(KeyError):
        await sch.resume(drifted, run.run_id, "ok")


async def test_two_nodes_park_in_the_same_wave():
    sch = Scheduler()
    run = await sch.run(build_two_approvals_plan())
    assert run.state == RunState.SUSPENDED
    assert set(run.interrupts) == {"a", "b"}

    run2 = await sch.resume(build_two_approvals_plan(), run.run_id, {"a": "yes-a", "b": "yes-b"})
    assert run2.state == RunState.COMPLETED
    assert run2.final_output == {"a": "yes-a", "b": "yes-b"}


async def test_resume_with_a_fresh_scheduler_shared_log():
    """Swap in a brand-new scheduler wired to the same event log, and it can
    still continue (a stand-in for a process restart): resume is a pure
    function of the recorded facts."""
    log = InMemoryEventLog()
    sch1 = Scheduler(eventlog=log)
    run = await sch1.run(build_plan())
    assert run.state == RunState.SUSPENDED

    sch2 = Scheduler(eventlog=log)
    run2 = await sch2.resume(build_plan(), run.run_id, "ok")
    assert run2.state == RunState.COMPLETED
    assert run2.final_output == "ok"


async def test_resume_after_a_mid_wave_crash():
    """A run that died with its node in flight replays as RUNNING; resume
    re-arms the node and re-drives it — at-least-once, so a park-first node
    asks again rather than receiving a never-recorded answer."""
    log = InMemoryEventLog()
    sch = Scheduler(eventlog=log)
    run = await sch.run(build_plan())
    assert run.state == RunState.SUSPENDED

    # Simulate the crash cut: the interruption never landed — the node was
    # still in flight when the process died.
    events = await log.events(run.run_id)
    log._streams[run.run_id] = [e for e in events if e.kind != "interrupted"]

    run2 = await sch.resume(build_plan(), run.run_id)  # nothing was parked: no value to feed
    assert run2.state == RunState.SUSPENDED  # the node honestly re-parked
    run3 = await sch.resume(build_plan(), run2.run_id, {"approved": True})
    assert run3.state == RunState.COMPLETED
    assert run3.final_output == {"approved": True}


async def test_finished_run_refuses_resume():
    """Completed history is not resumable — the facts are closed."""
    sch = Scheduler()
    done = Plan().add(Node("done", FnBody(lambda x, ctx: Outcome.ok("x")), terminal=True))
    run = await sch.run(done)
    assert run.state == RunState.COMPLETED
    with pytest.raises(RuntimeError):
        await sch.resume(done, run.run_id, "again")
