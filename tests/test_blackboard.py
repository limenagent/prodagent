"""Blackboard pattern: heterogeneous experts write a shared channel in parallel, a join=all moderator converges over multiple rounds."""

from src.kernel import (
    Bus,
    FnBody,
    InMemoryEventLog,
    InMemoryStore,
    Outcome,
    Scheduler,
)
from src.runtime.multiagent import build_blackboard

NAMES = ["alice", "bob", "carol"]


def make_expert(name):
    async def body(_, ctx):
        r = ctx.shared["round"]
        # deterministic for teaching: carol objects in round 0, is convinced in round 1 — verifies "converges over rounds".
        vote = "agree" if (name != "carol" or r >= 1) else "object"
        return Outcome.ok(name, board=[f"{name}:{vote}"])

    return FnBody(body)


async def moderator(_, ctx):
    r = ctx.shared["round"]
    last_votes = [b.split(":", 1)[1] for b in ctx.shared["board"][-len(NAMES) :]]
    if all(v == "agree" for v in last_votes):
        return Outcome.goto("final", verdict="consensus")
    return Outcome.goto("fanout", round=r + 1)  # not yet reached: back-edge for another round


async def test_blackboard_converges_after_two_rounds():
    plan = build_blackboard(
        [(n, make_expert(n)) for n in NAMES],
        FnBody(moderator),
    )
    sch = Scheduler(bus=Bus(), eventlog=InMemoryEventLog(), store=InMemoryStore())
    run = await sch.run(plan, task="评审")
    assert run.state.name == "COMPLETED"
    assert run.shared["verdict"] == "consensus"
    assert run.shared["round"] == 1  # confirms it actually took two rounds, not a no-op first round
    assert (
        len(run.shared["board"]) == 6
    )  # three experts x two rounds, every vote stays on the board


async def test_blackboard_single_round_when_all_agree():
    async def all_agree(_, ctx):
        return Outcome.goto("final", verdict="consensus")

    plan = build_blackboard([(n, make_expert(n)) for n in ["alice", "bob"]], FnBody(all_agree))
    sch = Scheduler(bus=Bus(), eventlog=InMemoryEventLog(), store=InMemoryStore())
    run = await sch.run(plan, task="评审")
    assert run.shared["verdict"] == "consensus"
    assert run.shared["round"] == 0
