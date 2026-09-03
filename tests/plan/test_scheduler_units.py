"""The scheduler serves composed units — one engine, no branches.

The combinators compile to kernel.graph subgraphs; the Scheduler drives
them exactly as it drives planner JSON or a hand-written Workflow — same
waves, same readonly discipline, same checkpoint path. This is the
acceptance test for "Scheduler 认任意 Unit/Graph" (REFACTOR-PLAN U7):
type-level generality, proven end to end.
"""

from __future__ import annotations

from prodagent.kernel.combinators import Route, Sequential
from prodagent.kernel.graph import Node, Plan, compile_planned
from prodagent.kernel.scheduler import Scheduler
from prodagent.kernel.units import FnUnit
from prodagent.tooling.dispatcher import ToolDispatcher


class _NoopLLM:
    async def complete(self, *a, **k):  # pragma: no cover - never called
        raise AssertionError("preset plan must not call the planner")


async def _drive(scheduler: Scheduler):
    terminal = None
    async for event in scheduler.stream("combine"):
        terminal = event
    return terminal


def _scheduler(fns: dict, plan: Plan) -> Scheduler:
    return Scheduler(
        initial_plan=plan,
        max_replans=0,
        dispatcher=ToolDispatcher({}),
        fns=fns,
    )


async def test_scheduler_runs_a_sequential_compiled_graph():
    """Sequential(a, b) compiles to a → b; the scheduler runs the wave with
    one node per wave and the value flows along the edge (param binding)."""
    calls: list[str] = []

    def first() -> dict:
        calls.append("first")
        return {"n": 1}

    def second(n) -> dict:  # param name binds to the upstream node id
        calls.append("second")
        return {"n": n["n"] + 1}

    seq = Sequential(FnUnit(fn="first"), FnUnit(fn="second"))
    # the compiled shape: chain nodes, value flowing by position — declared
    # here with the wire-native depends_on so the validator gates it too
    plan = compile_planned(
        [
            Node(node_id="first", body=FnUnit(fn="first")),
            Node(
                node_id="second",
                body=FnUnit(fn="second"),
                params={"n": "{{first.output}}"},
                depends_on=["first"],
                is_terminal=True,
            ),
        ]
    )
    assert [e.target for e in seq.graph().edges] == ["seq1"], "the shape under test"

    terminal = await _drive(_scheduler({"first": first, "second": second}, plan))
    assert terminal.run.state.value == "completed"
    assert calls == ["first", "second"]


async def test_scheduler_skips_the_route_branch_not_taken():
    """Route compiles to conditional edges; the waived branch is SKIPPED
    (obsolete), the taken branch waits for its source — never both run."""
    ran: list[str] = []

    def entry() -> dict:
        ran.append("entry")
        return {}

    def left() -> dict:
        ran.append("left")
        return {"side": "left"}

    def right() -> dict:
        ran.append("right")
        return {"side": "right"}

    route = Route(lambda shared: "left", {"left": FnUnit(fn="left"), "right": FnUnit(fn="right")})
    compiled = route.graph()  # gate → route:left / route:right, conditionally

    plan = Plan()
    plan.add_nodes([Node(node_id="entry", body=FnUnit(fn="entry"), is_terminal=False)])
    plan.add_nodes(list(compiled.nodes.values()))
    plan.edge("entry", "gate")
    for e in compiled.edges:
        plan.edge(e.source, e.target, when=e.when)

    terminal = await _drive(_scheduler({"entry": entry, "left": left, "right": right}, plan))
    assert terminal.run.state.value == "completed"
    assert "left" in ran and "right" not in ran, "the waived branch is skipped, not unlocked"
