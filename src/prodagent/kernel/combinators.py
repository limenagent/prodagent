"""Combinators — structured composition, one NodeBody nesting another.

Sequential / Parallel / Route / Loop: the four shapes 90% of orchestration
needs, all NodeBody implementations themselves, so they nest arbitrarily
(``Sequential(Parallel(a, b), Loop(Route(...)))``) — the Composite the
macro-node/micro-agent split never allowed.

Two execution forms, by ruling 2 of REFACTOR-PLAN:

- **Interpreted** (``run``): every combinator drives its children
  directly. Sequential feeds each child the previous outcome's value;
  Parallel races them under a join; Route picks by reading the *full*
  shared state (content-relevant choice, not name matching — this is the
  blackboard recipe's primitive); Loop iterates until its predicate or
  cap. This is the primary form, and the ONLY form Loop has.
- **Compiled** (``graph``): Sequential / Parallel / Route also compile to
  an acyclic subgraph, so embedded under the scheduler they join the wave
  world (readonly concurrency, replanning, checkpointing) as ordinary
  nodes and edges. Loop never compiles — a back-edge would break the
  acyclicity law; loops come from *this* interpretation, never from edges.
  Compiled Parallel is join-as-barrier (every child lands); the
  winner-cancels-loser joins (AnyOf / NOf) keep their cancel semantics in
  the interpreted form only.

Control is honest about commands: a child whose value IS a command (an
``Update``, a ``Goto``, a ``Handoff``) abandons the combinator — the
remaining children never run, and the command becomes the combinator's own
value, one level up. Same rule the node driver applies to a single body's
outcome; composition just honors it early.
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, Any

from prodagent.kernel.body import NodeBody, NodeContext, Outcome
from prodagent.kernel.command import Command

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping, Sequence

    from prodagent.kernel.graph import Graph

logger = logging.getLogger(__name__)


# ════════════ joins — how Parallel collects ════════════


class AllOf:
    """Every child must land; values in declaration order."""

    def satisfied(self, values: Sequence[Any], arity: int) -> bool:
        return len(values) >= arity

    @property
    def label(self) -> str:
        return "all_of"


class AnyOf:
    """First child to land wins; the rest are cancelled — first-winner races."""

    def satisfied(self, values: Sequence[Any], arity: int) -> bool:
        return len(values) >= 1

    @property
    def label(self) -> str:
        return "any_of"


class NOf:
    """``k`` children landing wins (quorum); the rest are cancelled."""

    def __init__(self, k: int) -> None:
        self.k = k

    def satisfied(self, values: Sequence[Any], arity: int) -> bool:
        return len(values) >= self.k

    @property
    def label(self) -> str:
        return f"{self.k}_of"


class Custom:
    """Arbitrary predicate over the landed values — dynamic fan-out's
    landing rule (Send's spirit, expressed as a join)."""

    def __init__(self, fn: Callable[[Sequence[Any]], bool]) -> None:
        self.fn = fn

    def satisfied(self, values: Sequence[Any], arity: int) -> bool:
        return bool(self.fn(values))

    @property
    def label(self) -> str:
        return "custom"


Join = AllOf | AnyOf | NOf | Custom


def _merge_deltas(outcomes: Sequence[Outcome]) -> dict[str, Any]:
    """Fold children's state deltas into one combinator-level delta (the
    driver applies it under the same rules as a single unit's)."""
    delta: dict[str, Any] = {}
    for oc in outcomes:
        delta.update(oc.state_delta)
    return delta


def _command_of(outcomes: Sequence[Outcome]) -> Command | None:
    """The first command-valued outcome — a command outranks collection:
    once any child redirects the run (an Update, a requeue, a handoff),
    nobody collects plain values anymore."""
    for oc in outcomes:
        if isinstance(oc.value, Command):
            return oc.value
    return None


def _collect(outcomes: Sequence[Outcome], command: Command | None = None) -> Outcome:
    if command is not None:
        return Outcome(value=command, state_delta=_merge_deltas(outcomes))
    return Outcome(value=[oc.value for oc in outcomes], state_delta=_merge_deltas(outcomes))


# ════════════ the combinators ════════════


class Sequential:
    """One after another; each child's ``value`` is the next child's input,
    the last value is the sequence's. A handoff anywhere abandons the rest."""

    readonly = None  # the chain runs children one at a time anyway
    kind = "sequential"

    def __init__(self, *units: NodeBody) -> None:
        self.units: tuple[NodeBody, ...] = units

    @property
    def target(self) -> str:
        return "sequential"

    async def run(self, input: Any, ctx: NodeContext) -> Outcome:
        value = input
        outcomes: list[Outcome] = []
        for unit in self.units:
            outcome = await unit.run(value, ctx)
            outcomes.append(outcome)
            if isinstance(outcome.value, Command):
                # A command IS the outcome (an Update, a requeue, a
                # handoff): the sequence ends here, the command bubbles up.
                return Outcome(value=outcome.value, state_delta=_merge_deltas(outcomes))
            value = outcome.value
        return Outcome(value=value, state_delta=_merge_deltas(outcomes))

    def graph(self) -> Graph:
        """The chain as an acyclic subgraph: u0 → u1 → …, value flowing
        along the edges by position."""
        from prodagent.kernel.graph import Graph, Node

        g = Graph()
        ids: list[str] = []
        for i, unit in enumerate(self.units):
            nid = f"seq{i}"
            ids.append(nid)
            g.add_nodes([Node(node_id=nid, body=unit)])
        for a, b in zip(ids, ids[1:], strict=False):
            g.edge(a, b)
        return g


class Parallel:
    """Children race concurrently; ``join`` decides when enough has landed.
    Values arrive in declaration order regardless of completion order. A
    child that raises fails the whole race (no hidden failure policy —
    tolerate-per-child is the unit's own business)."""

    readonly = None
    kind = "parallel"

    def __init__(self, *units: NodeBody, join: Join | None = None) -> None:
        self.units: tuple[NodeBody, ...] = units
        self.join: Join = join or AllOf()

    @property
    def target(self) -> str:
        return "parallel"

    async def run(self, input: Any, ctx: NodeContext) -> Outcome:
        tasks = [asyncio.ensure_future(unit.run(input, ctx)) for unit in self.units]
        # Keep task→slot adjacency so values report in declaration order.
        by_index = {id(t): i for i, t in enumerate(tasks)}
        landed: dict[int, Outcome] = {}
        pending = set(tasks)
        try:
            while pending:
                done, pending = await asyncio.wait(pending, return_when=asyncio.FIRST_COMPLETED)
                for task in done:
                    landed[by_index[id(task)]] = task.result()
                ordered = [landed[i] for i in sorted(landed)]
                if (command := _command_of(ordered)) is not None:
                    return _collect(ordered, command=command)
                if self.join.satisfied([o.value for o in ordered], len(self.units)):
                    break
        finally:
            for task in pending:
                task.cancel()
            if pending:
                await asyncio.gather(*pending, return_exceptions=True)
        ordered = [landed[i] for i in sorted(landed)]
        if (command := _command_of(ordered)) is not None:
            return _collect(ordered, command=command)
        return _collect(ordered)

    def graph(self) -> Graph:
        """Fork/join as an acyclic subgraph: gate → each child → barrier.
        The barrier's dependency on every child IS the join — compiled
        form is AllOf-shaped (see module docstring)."""
        from prodagent.kernel.graph import Graph, Node

        g = Graph()
        g.add_nodes(
            [
                Node(node_id="gate", body=_Gate(label=f"fan({self.join.label})")),
                Node(node_id="barrier", body=_Gate(label=f"join({self.join.label})")),
            ]
        )
        for i, unit in enumerate(self.units):
            nid = f"par{i}"
            g.add_nodes([Node(node_id=nid, body=unit)])
            g.edge("gate", nid)
            g.edge(nid, "barrier")
        return g


class Route:
    """Pick one target by reading the FULL shared state — the selector sees
    content, not event names; that is what makes the blackboard recipe
    expressible (scheduler-as-code or scheduler-as-LLM both fit the slot)."""

    readonly = None
    kind = "route"

    def __init__(
        self,
        selector: Callable[[Mapping[str, Any]], str],
        targets: dict[str, NodeBody],
    ) -> None:
        self.selector = selector
        self.targets: dict[str, NodeBody] = dict(targets)

    @property
    def target(self) -> str:
        return "route"

    async def run(self, input: Any, ctx: NodeContext) -> Outcome:
        key = self.selector(ctx.shared)
        unit = self.targets.get(key)
        if unit is None:
            raise KeyError(
                f"route selector returned {key!r}; known targets: {sorted(self.targets)}"
            )
        return await unit.run(input, ctx)

    def graph(self) -> Graph:
        """Conditional edges to every target — the selector picks at
        readiness time; waived branches are skipped by the graph itself."""
        from prodagent.kernel.graph import Graph, Node

        g = Graph()
        g.add_nodes([Node(node_id="gate", body=_Gate(label="route"))])
        for key, unit in self.targets.items():
            nid = f"route:{key}"
            g.add_nodes([Node(node_id=nid, body=unit)])
            g.edge("gate", nid, when=self._picks(key))
        return g

    def _picks(self, key: str) -> Callable[[Mapping[str, Any]], bool]:
        def check(shared: Mapping[str, Any]) -> bool:
            return self.selector(shared) == key

        return check


class Loop:
    """Iterate one unit until ``until`` holds or the iteration cap dies.
    Each iteration's ``value`` feeds the next iteration's input —
    refinement loops read naturally. Two forms, one shape (ruling 2,
    reversed — cycles are legal now): the interpreted form (``run``) keeps
    the exact iteration cap; the compiled form (:meth:`graph`) is the body
    plus a tail gate plus one back edge, active while ``until`` doesn't
    hold, leaning on the engine's guards for the loop that never exits."""

    readonly = False
    kind = "loop"

    def __init__(
        self,
        unit: NodeBody,
        until: Callable[[Mapping[str, Any]], bool],
        *,
        max_iterations: int = 10,
    ) -> None:
        self.unit = unit
        self.until = until
        self.max_iterations = max_iterations

    @property
    def target(self) -> str:
        return "loop"

    def graph(self) -> Graph:
        """The compiled shape: body → tail gate, and a back edge tail → body
        that stays active until ``until`` holds.

        The gate exists because a self-edge cannot bootstrap (a node
        waiting on itself never becomes ready): the tail runs after each
        body pass, and its back edge is what requeues the body. When the
        edge waives, the body stays COMPLETED and the loop ends. The
        interpreted form's exact ``max_iterations`` has no compiled
        counterpart — a cycle whose body never writes what ``until`` reads
        is the no-progress detector's to kill, loudly."""
        from prodagent.kernel.graph import Graph, Node, Origin

        g = Graph(origin=Origin.DYNAMIC)
        g.add_nodes(
            [
                Node(node_id="loop_body", body=self.unit, origin=Origin.DYNAMIC),
                Node(node_id="loop_tail", body=_Gate("loop"), origin=Origin.DYNAMIC),
            ]
        )
        g.edge("loop_body", "loop_tail")
        g.edge("loop_tail", "loop_body", when=lambda shared: not self.until(shared))
        return g

    async def run(self, input: Any, ctx: NodeContext) -> Outcome:
        value = input
        outcomes: list[Outcome] = []
        for _ in range(self.max_iterations):
            outcome = await self.unit.run(value, ctx)
            outcomes.append(outcome)
            if isinstance(outcome.value, Command):
                # A command IS the outcome: the loop ends here, the command
                # (a handoff, a requeue) bubbles up.
                return Outcome(value=outcome.value, state_delta=_merge_deltas(outcomes))
            value = outcome.value
            # until sees the shared state (the blackboard) plus every delta
            # so far — a fresh key can end the loop the round it was written.
            view: dict[str, Any] = {**dict(ctx.shared), **_merge_deltas(outcomes)}
            if self.until(view):
                break
        else:
            logger.warning(
                "loop hit its iteration cap (%d) before until() held", self.max_iterations
            )
        return Outcome(value=value, state_delta=_merge_deltas(outcomes))


# ════════════ compile-only synthetic nodes ════════════


class _Gate:
    """Synthetic node of a compiled shape (Parallel's fan/join, Route's
    gate) — runs nothing and carries no value (its output is never
    referenced; routing is the edges' business), labels the shape in its
    target so events say what they passed through."""

    readonly = True
    kind = "gate"

    def __init__(self, label: str) -> None:
        self.label = label

    @property
    def target(self) -> str:
        return self.label

    async def run(self, input: Any, ctx: NodeContext) -> Outcome:
        return Outcome(value=None)


__all__ = [
    "Sequential",
    "Parallel",
    "Route",
    "Loop",
    "AllOf",
    "AnyOf",
    "NOf",
    "Custom",
    "Join",
]
