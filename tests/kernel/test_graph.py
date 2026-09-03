"""Graph/Plan — topology split and conditional edges.

Three laws under test: (1) the edge set is the runtime truth — declared
``depends_on`` and edges stay in lockstep through every rewire; (2) a
conditional edge (``when``) is Route's underlying form — waived satisfies
the dependency without the source running, active blocks until COMPLETED;
(3) Graph is pure reusable topology — Plan layers version and lineage on
top without the graph ever learning about them.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from prodagent.kernel.bodies import FnBody
from prodagent.kernel.graph import Edge, Graph, Node, Plan, fresh_states, state_of
from prodagent.kernel.types import NodeStatus

if TYPE_CHECKING:
    from prodagent.kernel.node_state import NodeRuntimeState


def _fn(node_id: str, *deps: str) -> Node:
    return Node(node_id=node_id, body=FnBody(fn=node_id), depends_on=list(deps))


def _completed(states: dict[str, NodeRuntimeState], *node_ids: str) -> dict[str, NodeRuntimeState]:
    for nid in node_ids:
        states[nid].mark_running()
        states[nid].mark_completed({"ok": nid})
    return states


class TestEdgeSetIsTheTruth:
    def test_declared_deps_become_edges_at_add_time(self):
        g = Graph()
        g.add_nodes([_fn("a"), _fn("b", "a"), _fn("c", "b")])
        assert [(e.source, e.target) for e in g.edges] == [("a", "b"), ("b", "c")]
        assert g.deps_of("c") == ("b",)
        assert g.dependents_of("a") == ["b"]

    def test_explicit_edges_live_beside_declared_ones(self):
        g = Graph()
        g.add_nodes([_fn("a"), _fn("b")])
        g.edge("a", "b")
        assert g.deps_of("b") == ("a",)

    def test_ready_follows_the_edge_set(self):
        g = Graph()
        g.add_nodes([_fn("a"), _fn("b", "a")])
        states = fresh_states(g)
        assert [n.node_id for n in g.ready(states)] == ["a"]
        _completed(states, "a")
        assert [n.node_id for n in g.ready(states)] == ["b"]


class TestConditionalEdges:
    """``when`` is the predicate form of Route: the edge is active while
    the predicate holds against the shared state, waived otherwise."""

    def _route_graph(self) -> tuple[Graph, dict[str, NodeRuntimeState]]:
        g = Graph()
        g.add_nodes([_fn("entry"), _fn("left"), _fn("right")])
        g.edge("entry", "left", when=lambda shared: shared.get("pick") == "left")
        g.edge("entry", "right", when=lambda shared: shared.get("pick") == "right")
        return g, fresh_states(g)

    def test_waived_edge_skips_the_branch_it_feeds(self):
        g, states = self._route_graph()
        _completed(states, "entry")
        ready = {n.node_id for n in g.ready(states, shared={"pick": "left"})}
        assert ready == {"left"}, "right's edge is waived — right never becomes ready"
        assert [n.node_id for n in g.skipped(states, shared={"pick": "left"})] == ["right"]

    def test_active_edge_still_blocks_until_completed(self):
        g, states = self._route_graph()
        assert [n.node_id for n in g.ready(states, shared={"pick": "left"})] == ["entry"], (
            "only the root is ready; left waits on its active edge"
        )

    def test_route_branch_runs_then_its_sibling_no_longer_blocks_the_sink(self):
        g = Graph()
        g.add_nodes([_fn("entry"), _fn("sink")])
        g.edge("entry", "sink")
        g.edge("entry", "sink", when=lambda shared: False)  # duplicate waived edge
        states = fresh_states(g)
        _completed(states, "entry")
        assert [n.node_id for n in g.ready(states)] == ["sink"]

    def test_hard_edge_is_always_active(self):
        e = Edge(source="a", target="b")
        assert e.is_active(None) is True
        assert e.is_active({}) is True


class TestGraphPlanSplit:
    def test_derive_shares_nodes_and_edges_but_not_identity(self):
        template = Plan()
        template.add_nodes([_fn("a"), _fn("b", "a")])
        template.version = 3

        run_plan = template.derive(plan_id="run-1", task_input="do it")

        assert run_plan.plan_id == "run-1"
        assert run_plan.task_input == "do it"
        assert run_plan.version == 3
        assert run_plan.get_node("a") is template.get_node("a"), "frozen nodes are shared"
        assert run_plan.deps_of("b") == template.deps_of("b"), "edges carry over"
        assert run_plan is not template

    def test_wire_round_trips_through_edges(self):
        plan = Plan()
        plan.add_nodes([_fn("a"), _fn("b", "a")])
        states = fresh_states(plan)
        _completed(states, "a")

        wire = plan.to_state(states)
        rebuilt, rebuilt_states = Plan.from_state(wire, plan_id="restored")

        assert rebuilt.deps_of("b") == ("a",)
        assert state_of(rebuilt_states, "a").status is NodeStatus.COMPLETED
        assert state_of(rebuilt_states, "b").status is NodeStatus.PENDING


class TestRunUnitRefAndCursor:
    def test_unit_ref_rides_the_checkpoint_and_defaults_empty(self):
        from prodagent.kernel.run import Run

        run = Run(run_id="r1", task="t", unit_ref="triage")
        wire = run.to_dict()
        assert wire["unit_ref"] == "triage"
        restored = Run.from_dict(wire)
        assert restored.unit_ref == "triage"

        legacy = Run.from_dict({"run_id": "r2", "task": "t"})  # pre-unit_ref checkpoint
        assert legacy.unit_ref == ""

    def test_plan_cursor_round_trips_typed(self):
        from prodagent.kernel.run import Run, SchedulerCursor

        run = Run(run_id="r1", task="t")
        run.set_plan_cursor(SchedulerCursor(state={"version": 2, "nodes": {}}, last_seq=7))

        cursor = run.plan_cursor()
        assert cursor.last_seq == 7
        assert cursor.state == {"version": 2, "nodes": {}}
        # the wire shape is the historical dict — old checkpoints load unchanged
        assert run.cursor("plan") == {"state": {"version": 2, "nodes": {}}, "last_seq": 7}
