"""Interrupt: a node requests to let go and pause, checkpoint is persisted; resume feeds back an external value, re-runs the parked node and continues."""

from src.kernel import FnBody, InMemoryStore, Node, Outcome, Plan, RunState, Scheduler


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


async def test_two_nodes_park_in_the_same_wave():
    sch = Scheduler()
    run = await sch.run(build_two_approvals_plan())
    assert run.state == RunState.SUSPENDED
    assert set(run.interrupts) == {"a", "b"}

    run2 = await sch.resume(build_two_approvals_plan(), run.run_id, {"a": "yes-a", "b": "yes-b"})
    assert run2.state == RunState.COMPLETED
    assert run2.final_output == {"a": "yes-a", "b": "yes-b"}


async def test_resume_with_a_fresh_scheduler_shared_store():
    """Swap in a brand-new scheduler wired to the same checkpoint store, and it can still continue (a stand-in for a process restart)."""
    store = InMemoryStore()
    sch1 = Scheduler(store=store)
    run = await sch1.run(build_plan())
    assert run.state == RunState.SUSPENDED

    sch2 = Scheduler(store=store)
    run2 = await sch2.resume(build_plan(), run.run_id, "ok")
    assert run2.state == RunState.COMPLETED
    assert run2.final_output == "ok"
