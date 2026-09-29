"""Blackboard pattern: heterogeneous experts write a shared channel in parallel, a join=all moderator converges over rounds.

Assembled with the public Workflow facade (fan-out back-edges via Goto.rejoin) —
the same structure as examples/blackboard.py, with scripted FnBody experts so it
runs offline and deterministically.
"""

from src import Workflow, append, go, last
from src.kernel import Goto, Outcome

NAMES = ["alice", "bob", "carol"]


def make_expert(name):
    async def body(_, ctx):
        r = ctx.shared["round"]
        # deterministic for teaching: carol objects in round 0, is convinced in round 1 — verifies "converges over rounds".
        vote = "agree" if (name != "carol" or r >= 1) else "object"
        return {"board": [f"{name}:{vote}"]}

    return body


def moderator(_history):
    async def body(_, ctx):
        r = ctx.shared["round"]
        last_votes = [b.split(":", 1)[1] for b in ctx.shared["board"][-len(NAMES) :]]
        if all(v == "agree" for v in last_votes):
            return go("final", verdict="consensus")
        return go("fanout", round=r + 1)  # not yet reached: back-edge for another round

    return body


def build_blackboard(names, moderate):
    wf = Workflow()
    wf.channel("board", append())
    wf.channel("round", last(0))
    wf.channel("verdict", last(None))

    async def fanout(_, ctx):
        # Re-arm without immediate activation: parallel timing is set by the
        # fanout→expert edges, join timing by expert→moderator, every round.
        return Outcome(control=[Goto.rejoin(n) for n in (*names, "moderator")])

    wf.add_node("fanout", fanout)
    for name in names:
        wf.add_node(name, make_expert(name))
        wf.add_edge("fanout", name)
        wf.add_edge(name, "moderator")
    wf.add_node("moderator", moderate, join="all")
    wf.add_node("final", lambda _, ctx: ctx.shared["verdict"], terminal=True)
    wf.entry("fanout")
    return wf


async def test_blackboard_converges_after_two_rounds():
    wf = build_blackboard(NAMES, moderator(NAMES))
    r = await wf.run("评审")
    assert r.status == "completed"
    assert r.state["verdict"] == "consensus"
    assert r.state["round"] == 1  # confirms it actually took two rounds, not a no-op first round
    assert len(r.state["board"]) == 6  # three experts x two rounds, every vote stays on the board


async def test_blackboard_single_round_when_all_agree():
    async def all_agree(_, ctx):
        return go("final", verdict="consensus")

    wf = build_blackboard(["alice", "bob"], all_agree)
    r = await wf.run("评审")
    assert r.state["verdict"] == "consensus"
    assert r.state["round"] == 0
