"""Handoff (transfer) assembled from Workflow primitives: `go` to another Agent
node with no return edge. A chain hands off in a fixed order; a swarm net can
hand the case back to a peer, and the graph has no supervisor node."""

from examples.handoff import build_chain, build_swarm, parse_verdict
from src.kernel import NodeStatus, RunState


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


def test_parse_verdict_takes_the_last_next_line_not_the_prose():
    """A real model surrounds its verdict with prose — a whole triage note
    before it, sometimes a second NEXT after it. The routing fact is the final
    NEXT line: rpartition anchors there, so the go target is the bare node
    name, never the surrounding paragraphs."""
    target, summary = parse_verdict(
        "分诊记录 / Triage Summary\n\n案件信息\n- 订单号：O-1234\n"
        "- 问题类型：扣款成功但未收到货\n\n路由决定：移交账单专员核实。\n\n"
        "NEXT:close :: 已核实扣款¥399且未发货\n\n"
        "已通过账务系统核实：扣款 ¥399、未发货，属未履约订单。\n\n"
        "NEXT:close :: 核实订单O-1234已扣款¥399且未发货，批准全额退款，结案。"
    )
    assert target == "close"
    assert summary == "核实订单O-1234已扣款¥399且未发货，批准全额退款，结案。"


def test_parse_verdict_fails_loud_on_bad_shapes():
    """The boundary promise: a malformed verdict reports 'bad verdict' here —
    never a misleading 'Goto target does not exist' from routing raw prose."""
    import pytest

    with pytest.raises(ValueError, match="must end with"):
        parse_verdict("just analysis, no verdict line")  # no NEXT: at all
    with pytest.raises(ValueError, match="bad verdict target"):
        parse_verdict("NEXT:billing and risk :: split the work")  # not a bare node
    with pytest.raises(ValueError, match="bad verdict target"):
        parse_verdict("NEXT: :: missing node")  # empty target


async def test_a_specialist_settling_early_skips_the_unreached_peer():
    """The swarm's promise: whoever can settle, settles — even if the other
    peer never touched the case. The gated in-edge turns the never-handed
    specialist into a dead branch the sweep skips; without it the run stalls
    on the pending peer (exactly what a real model that jumped straight to
    close used to produce)."""
    from examples.handoff import _close, _handed, _texts, specialist, specialist_node
    from src import Workflow, append, merge

    t = _texts("en")
    billing = specialist(
        "billing",
        ["NEXT:close :: Verified the charge — straightforward refund, I settle it."],
        t=t,
        peers=("risk", "close"),
    )
    risk = specialist("risk", ["NEXT:close :: never reached"], t=t, peers=("billing", "close"))

    wf = Workflow()
    wf.channel("trail", append())
    wf.channel("handed", merge())
    wf.add_node("billing", specialist_node(billing))
    wf.add_node("risk", specialist_node(risk))
    wf.add_node("close", _close, terminal=True)
    wf.add_edge("billing", "risk", when=_handed("risk"))
    wf.entry("billing")

    r = await wf.run("refund for O-1234")
    assert r.run.state == RunState.COMPLETED
    assert r.state["trail"] == ["billing -> close"]
    assert r.run.state_of("risk").status == NodeStatus.SKIPPED
    assert r.run.state_of("billing").attempts == 1


async def test_a_missing_verdict_line_gets_one_repair_round():
    """A real model sometimes writes the whole analysis and forgets the NEXT
    line entirely — the routing fact is nowhere in the reply. The boundary
    refuses to guess; the node feeds the format error back once and re-runs
    the specialist, and the second reply routes. ScriptedLlm pops replies in
    order, so the repair round is deterministic offline: reply 1 has no NEXT,
    reply 2 does."""
    from examples.handoff import _close, _handed, _texts, specialist, specialist_node
    from src import Workflow, append, merge

    t = _texts("en")
    billing = specialist(
        "billing",
        [
            "Verified: charged ¥399 and not shipped. The refund needs the fraud check first.",
            "NEXT:risk :: Charged ¥399 and not shipped; fraud check needed.",
        ],
        t=t,
        peers=("risk", "close"),
    )
    risk = specialist(
        "risk", ["NEXT:close :: Clean; approve the ¥399 refund."], t=t, peers=("close",)
    )

    wf = Workflow()
    wf.channel("trail", append())
    wf.channel("handed", merge())
    wf.add_node("billing", specialist_node(billing))
    wf.add_node("risk", specialist_node(risk))
    wf.add_node("close", _close, terminal=True)
    wf.add_edge("billing", "risk", when=_handed("risk"))
    wf.entry("billing")

    r = await wf.run("refund for O-1234")
    assert r.run.state == RunState.COMPLETED
    # the repair round routed on the second reply, then risk settled
    assert r.state["trail"] == ["billing -> risk", "risk -> close"]
    assert r.run.state_of("billing").attempts == 1  # repaired inside one node execution
