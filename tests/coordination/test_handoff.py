"""handoff — the one control-transfer word, vocabulary to wire.

Laws: ``handoff()`` produces the kernel Outcome (control=Handoff, target
live in-process or a name); a node returning that control parks the run
exactly like the tool path (run completes, PendingHandoff set, first
handoff wins); ``target_name`` maps a live unit to its wire name and
refuses a nameless one; ``lower_to_activation`` states the relay's pure
data form once.
"""

from __future__ import annotations

from typing import Any

import pytest

from prodagent.coordination.handoff import handoff, handoff_of, lower_to_activation, target_name
from prodagent.kernel.run import PendingHandoff, RunState
from prodagent.kernel.unit import Handoff, Outcome, Return


class _Taker:
    """A named unit a handoff can point at."""

    name = "reviewer"
    readonly = False
    kind = "taker"

    @property
    def target(self) -> str:
        return "reviewer"

    async def run(self, input: Any, ctx: Any) -> Outcome:
        return Outcome(value="took over")


class TestVocabulary:
    def test_handoff_is_the_control_word(self):
        taker = _Taker()
        outcome = handoff(taker, task="review the draft")
        control = handoff_of(outcome)
        assert isinstance(control, Handoff)
        assert control.target is taker
        assert control.carry == "full"

    def test_a_name_target_stays_a_name(self):
        outcome = handoff("reviewer", task="review", carry="filtered")
        control = handoff_of(outcome)
        assert isinstance(control, Handoff)
        assert control.target == "reviewer"
        assert control.carry == "filtered"

    def test_handoff_of_a_plain_outcome_is_none(self):
        assert handoff_of(Outcome(value="ok")) is None
        assert handoff_of(Outcome(value="x", control=Return())) is None

    def test_target_name_maps_live_units(self):
        assert target_name(_Taker()) == "reviewer"
        assert target_name("reviewer") == "reviewer"

    def test_a_nameless_target_cannot_cross(self):
        class _Anon:
            readonly = True
            kind = "anon"

            @property
            def target(self):
                return ""

            async def run(self, input, ctx):
                return Outcome()

        with pytest.raises(ValueError, match="carries no name"):
            target_name(_Anon())


class TestLowering:
    def test_pending_handoff_lowers_to_pure_data(self):
        pending = PendingHandoff(
            peer_name="reviewer", task="review the draft", peer_run_id="root::reviewer"
        )
        activation = lower_to_activation(pending)
        assert activation.peer_name == "reviewer"
        assert activation.task == "review the draft"
        assert activation.run_id == "root::reviewer"
        # pure data: every field is a str/int — nothing live crosses
        assert all(isinstance(v, (str, int, type(None))) for v in activation.__dataclass_fields__)


class TestNodeParksOnControlHandoff:
    async def test_a_node_returning_handoff_control_parks_the_run(self):
        from prodagent.kernel.graph import Node, Plan
        from prodagent.kernel.node_runner import NodeHandoff, NodeRunner

        class _Handoffer:
            readonly = False
            kind = "handoffer"

            @property
            def target(self) -> str:
                return "handoffer"

            async def run(self, input: Any, ctx: Any) -> Outcome:
                return handoff("reviewer", task="take it from here")

        plan = Plan()
        plan.add_nodes([Node(node_id="h", body=_Handoffer(), is_terminal=True)])
        runner = NodeRunner(None)
        run = await _fresh_run("handoff-run")
        outcome = await runner.run_one(plan.get_node("h"), plan, run)

        assert isinstance(outcome, NodeHandoff)
        assert run.pending_handoff is not None
        assert run.pending_handoff.peer_name == "reviewer"
        assert run.state is RunState.COMPLETED  # park completes the run — control truly moved

    async def test_first_handoff_wins_across_nodes(self):
        from prodagent.kernel.run import Run

        run = Run(run_id="race", task="t")
        first = run.park_handoff(PendingHandoff(peer_name="first", task="a"))
        second = run.park_handoff(PendingHandoff(peer_name="second", task="b"))
        assert first is True and second is False
        assert run.pending_handoff.peer_name == "first"


async def _fresh_run(run_id: str):
    from prodagent.kernel.run import Run

    return Run(run_id=run_id, task="t")
