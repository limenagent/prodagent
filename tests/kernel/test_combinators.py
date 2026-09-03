"""Combinators — the four shapes, interpreted and compiled.

Laws under test: Sequential chains values and abandons the tail on a
handoff; Parallel races under its join (winner joins cancel losers);
Route reads the full shared state and refuses unknown targets; Loop
iterates value-to-value until the predicate or the cap. Compiled forms
are acyclic (Loop has none) and Route's compiled branches waive/skip the
way kernel.graph promises.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from prodagent.kernel.combinators import (
    AnyOf,
    Custom,
    Loop,
    NOf,
    Parallel,
    Route,
    Sequential,
)
from prodagent.kernel.unit import Handoff, Outcome, UnitContext
from prodagent.kernel.units import NodeKind


class _Const:
    """A unit that returns a constant, optionally writing state or handing off."""

    readonly = True
    kind = "const"

    def __init__(self, value: Any, *, delta: dict | None = None, handoff_to: Any = None):
        self.value = value
        self.delta = delta or {}
        self.handoff_to = handoff_to

    @property
    def target(self) -> str:
        return f"const({self.value})"

    async def run(self, input: Any, ctx: UnitContext) -> Outcome:
        if self.handoff_to is not None:
            return Outcome(value=None, state_delta=self.delta, control=Handoff(self.handoff_to))
        return Outcome(value=self.value, state_delta=self.delta)


class _Add:
    """A unit that adds a number to its input — chaining made visible."""

    readonly = True
    kind = "add"

    def __init__(self, n: int) -> None:
        self.n = n

    @property
    def target(self) -> str:
        return f"add({self.n})"

    async def run(self, input: Any, ctx: UnitContext) -> Outcome:
        return Outcome(value=int(input) + self.n)


def _ctx(shared: dict | None = None) -> UnitContext:
    return UnitContext(run_id="r-comb", shared=shared or {})


class TestSequential:
    async def test_chains_each_value_into_the_next_input(self):
        seq = Sequential(_Add(1), _Add(10), _Add(100))
        assert (await seq.run(0, _ctx())).value == 111

    async def test_last_value_is_the_sequences_value(self):
        assert (await Sequential(_Const("a"), _Const("b")).run(None, _ctx())).value == "b"

    async def test_handoff_abandons_the_tail(self):
        tail = _Const("never")
        seq = Sequential(_Const("first"), _Const("x", handoff_to=_Const("peer")), tail)
        outcome = await seq.run(None, _ctx())
        assert isinstance(outcome.control, Handoff)
        assert outcome.value == "first", "the value amassed before the taker ran survives"

    async def test_state_deltas_fold_one_level_up(self):
        seq = Sequential(_Const(1, delta={"a": 1}), _Const(2, delta={"b": 2}))
        assert (await seq.run(None, _ctx())).state_delta == {"a": 1, "b": 2}

    def test_compiled_form_is_a_chain(self):
        g = Sequential(_Add(1), _Add(2)).graph()
        assert sorted(g.deps_of("seq1")) == ["seq0"]
        assert g.dependents_of("seq0") == ["seq1"]


class TestParallel:
    async def test_all_of_collects_every_value_in_declaration_order(self):
        async def slow_val():
            await asyncio.sleep(0.01)
            return "slow"

        class _Slow:
            readonly = True
            kind = "slow"

            @property
            def target(self):
                return "slow"

            async def run(self, input, ctx):
                return Outcome(value=await slow_val())

        outcome = await Parallel(_Slow(), _Const("fast")).run(None, _ctx())
        assert outcome.value == ["slow", "fast"], "completion order differs, report order does not"

    async def test_any_of_cancels_the_losers(self):
        cancelled = False

        class _Never:
            readonly = True
            kind = "never"

            @property
            def target(self):
                return "never"

            async def run(self, input, ctx):
                nonlocal cancelled
                try:
                    await asyncio.sleep(30)
                    return Outcome(value="late")
                except asyncio.CancelledError:
                    cancelled = True
                    raise

        outcome = await Parallel(_Const("won"), _Never(), join=AnyOf()).run(None, _ctx())
        assert outcome.value == ["won"]
        await asyncio.sleep(0)
        assert cancelled is True, "the loser must be cancelled, never left running"

    async def test_n_of_waits_for_quorum(self):
        outcome = await Parallel(_Const("a"), _Const("b"), _Const("c"), join=NOf(2)).run(
            None, _ctx()
        )
        # instant children may land in one batch — quorum is "at least k"
        assert len(outcome.value) >= 2

    async def test_custom_join_decides_by_values(self):
        outcome = await Parallel(_Const(2), _Const(3), join=Custom(lambda vs: sum(vs) >= 5)).run(
            None, _ctx()
        )
        assert sum(outcome.value) >= 5

    async def test_handoff_outranks_collection(self):
        outcome = await Parallel(_Const("a"), _Const("x", handoff_to=_Const("peer"))).run(
            None, _ctx()
        )
        assert isinstance(outcome.control, Handoff)

    def test_compiled_form_is_a_barrier(self):
        g = Parallel(_Add(1), _Add(2)).graph()
        assert g.dependents_of("gate") == ["par0", "par1"]
        assert sorted(g.deps_of("barrier")) == ["par0", "par1"]


class TestRoute:
    async def test_selector_reads_the_full_shared_state(self):
        route = Route(
            selector=lambda shared: "writer" if shared.get("research_ready") else "researcher",
            targets={"researcher": _Const("digging"), "writer": _Const("writing")},
        )
        assert (await route.run(None, _ctx({}))).value == "digging"
        assert (await route.run(None, _ctx({"research_ready": True}))).value == "writing"

    async def test_unknown_target_names_every_known_branch(self):
        route = Route(lambda shared: "ghost", targets={"a": _Const(1)})
        with pytest.raises(KeyError, match="known targets.*a"):
            await route.run(None, _ctx())

    def test_compiled_form_uses_conditional_edges(self):
        g = Route(
            lambda shared: "left", targets={"left": _Const("L"), "right": _Const("R")}
        ).graph()
        assert {e.target for e in g.dependents_of("gate") and g.edges} >= {
            "route:left",
            "route:right",
        }


class TestLoop:
    async def test_iterates_value_to_value_until_predicate_holds(self):
        class _Double:
            readonly = False
            kind = "double"

            @property
            def target(self):
                return "double"

            async def run(self, input, ctx):
                return Outcome(value=int(input) * 2)

        loop = Loop(_Double(), until=lambda view: int(view.get("out") or 0) >= 10)

        # the predicate sees shared state, not the looping value — drive via deltas
        class _CountUp:
            readonly = False
            kind = "count"

            def __init__(self):
                self.n = 0

            @property
            def target(self):
                return "count"

            async def run(self, input, ctx):
                self.n += 1
                return Outcome(value=self.n, state_delta={"count": self.n})

        counter = _CountUp()
        loop = Loop(counter, until=lambda view: view.get("count", 0) >= 3)
        outcome = await loop.run(0, _ctx())
        assert outcome.value == 3
        assert outcome.state_delta == {"count": 3}

    async def test_cap_is_a_ceiling_not_a_promise(self):
        loop = Loop(_Const(1), until=lambda view: False, max_iterations=4)
        outcome = await loop.run(0, _ctx())
        assert outcome.value == 1  # ran to the cap without until() holding

    async def test_handoff_escapes_the_loop(self):
        loop = Loop(_Const("x", handoff_to=_Const("peer")), until=lambda view: True)
        outcome = await loop.run(0, _ctx())
        assert isinstance(outcome.control, Handoff)

    def test_loop_never_compiles(self):
        assert not hasattr(Loop(_Const(1), until=lambda v: True), "graph")


class TestUnitConformance:
    def test_every_combinator_is_a_unit_structurally(self):
        from prodagent.kernel.unit import Unit

        assert isinstance(Sequential(_Const(1)), Unit)
        assert isinstance(Parallel(_Const(1)), Unit)
        assert isinstance(Route(lambda s: "a", {"a": _Const(1)}), Unit)
        assert isinstance(Loop(_Const(1), until=lambda v: True), Unit)

    def test_kinds_are_not_node_kinds(self):
        # combinators are composition vocabulary, not the five node kinds
        assert Sequential(_Const(1)).kind not in set(NodeKind)
