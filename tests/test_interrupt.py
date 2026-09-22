"""Interrupt: a node requests to let go and pause, checkpoint is persisted; resume feeds back an external value, re-runs the parked node and continues."""

import pytest

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


async def test_resume_rejects_a_drifted_plan():
    """A checkpoint belongs to its blueprint: resuming against a Plan whose
    static node set differs fails loudly instead of mis-wiring silently."""
    sch = Scheduler()
    run = await sch.run(build_plan())
    assert run.state == RunState.SUSPENDED

    drifted = Plan().add(Node("elsewhere", FnBody(lambda x, ctx: x), terminal=True))
    with pytest.raises(ValueError):
        await sch.resume(drifted, run.run_id, "ok")


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


async def test_resume_does_not_rewrite_stored_history():
    """The suspension-time snapshot is history: the resumed run executes on
    copies, and the terminal save adds a NEW snapshot — the stored suspension
    object is never mutated in place (compare serializations of the captured
    object; a shallow load copy would hide in-place mutation)."""
    import json

    class Probe(InMemoryStore):
        def __init__(self):
            super().__init__()
            self.saved = []

        async def save(self, run_id, snapshot, **kw):
            self.saved.append(snapshot)
            return await super().save(run_id, snapshot, **kw)

    store = Probe()
    sch = Scheduler(store=store, durability="exit")
    run = await sch.run(build_plan())
    assert run.state == RunState.SUSPENDED
    frozen = json.dumps(await store.load(run.run_id))

    resumed = await sch.resume(build_plan(), run.run_id, "ok")
    assert resumed.state == RunState.COMPLETED
    # the terminal state is durable too — as a new save, not a rewrite of the old one
    assert (await store.load(run.run_id))["state"] == "completed"
    assert json.dumps(store.saved[0]) == frozen
