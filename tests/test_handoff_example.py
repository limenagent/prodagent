"""Handoff (transfer) assembled from Workflow primitives: `go` to another Agent
node with no return edge. A chain hands off in a fixed order; a swarm net can
hand the case back to a peer, and the graph has no supervisor node."""

from examples.handoff import build_chain, build_swarm
from src.kernel import RunState


async def test_handoff_chain_moves_forward_and_never_returns():
    r = await build_chain().run("charged but not shipped, refund for O-1234")
    assert r.run.state == RunState.COMPLETED
    # control visits each specialist once, then settles — nobody is re-entered
    assert r.state["trail"] == [
        "triage -> billing",
        "billing -> risk",
        "risk -> close",
    ]
    for n in ("triage", "billing", "risk"):
        assert r.run.state_of(n).attempts == 1  # handed off, control never comes back
    assert "399" in r.output


async def test_swarm_net_can_hand_back_without_a_supervisor():
    r = await build_swarm().run("charged but not shipped, refund for O-1234")
    assert r.run.state == RunState.COMPLETED
    # risk bounces the amount mismatch back to billing once, then it settles
    assert r.state["trail"] == [
        "billing -> risk",
        "risk -> billing",
        "billing -> risk",
        "risk -> close",
    ]
    assert r.run.state_of("billing").attempts == 2
    assert r.run.state_of("risk").attempts == 2
    # no triage, no supervisor — only two peers and the terminal node
    assert set(r.run.plan.nodes) == {"billing", "risk", "close"}
    assert "399" in r.output
