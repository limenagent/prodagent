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


class _LoopBodyStandIn:
    """Duck-typed LoopBody — kernel tests never import the recipes layer,
    and the binder reads only ``kind`` off the body."""

    kind = "loop"
    target = "loop"


class TestMarkerTail:
    """The marker stream's one tail — plan events and the loop recipe's
    round/terminal markers interleave on ``<run_id>``, so the plan cursor
    and the loop's box must hold ONE number."""

    def test_advance_moves_both_boxes_in_lockstep(self):
        from prodagent.kernel.run import MARKER_TAIL_CURSOR, Run

        run = Run(run_id="r1", task="t")
        assert run.marker_tail() == 0

        run.advance_marker_tail(4)

        assert run.marker_tail() == 4
        assert run.cursor(MARKER_TAIL_CURSOR) == 4
        assert run.plan_cursor().last_seq == 4, "the plan box moves with it"

    def test_max_reads_through_a_half_advanced_box(self):
        from prodagent.kernel.run import MARKER_TAIL_CURSOR, Run

        run = Run(run_id="r1", task="t")
        run.set_cursor(MARKER_TAIL_CURSOR, 3)  # legacy checkpoint: loop box only
        assert run.marker_tail() == 3


class TestRestoreBindsComposedBodies:
    """Resume of a preset graph whose work node is a loop — the 2026-09-04
    aiops incident: the binder returned the unit shape's body (None in the
    graph shape) and ``body_from_wire`` refused kind ``loop``, so the second
    spawn died in 20ms. The body's home is the process-local blueprint."""

    def _preset(self) -> Plan:
        from prodagent.kernel.bodies import LLMBody

        plan = Plan(plan_id="blueprint")
        plan.add_nodes(
            [
                Node(node_id="plan", body=LLMBody(prompt="make steps")),
                Node(
                    node_id="work", body=_LoopBodyStandIn(), is_terminal=True, depends_on=["plan"]
                ),
            ]
        )
        return plan

    def _incident_state(self) -> dict:
        """The wire exactly as the crashed run left it: plan COMPLETED,
        work PENDING with kind/action ``loop``."""
        return {
            "version": 1,
            "nodes": {
                "plan": {
                    "node_id": "plan",
                    "kind": "llm",
                    "action": "llm",
                    "origin": "static",
                    "prompt": "make steps",
                    "params": {},
                    "depends_on": [],
                    "is_terminal": False,
                    "status": "completed",
                    "output_ref": {"result": "steps"},
                },
                "work": {
                    "node_id": "work",
                    "kind": "loop",
                    "action": "loop",
                    "origin": "static",
                    "params": {"goal": "{{plan.output}}"},
                    "depends_on": ["plan"],
                    "is_terminal": True,
                    "status": "pending",
                    "output_ref": None,
                },
            },
        }

    def test_binder_takes_the_work_node_body_from_the_preset(self):
        from prodagent.kernel.scheduler import Scheduler

        preset = self._preset()
        scheduler = Scheduler(initial_plan=preset)

        body = scheduler._restore_binder({"node_id": "work", "kind": "loop", "action": "loop"})

        assert body is preset.get_node("work").body

    def test_incident_checkpoint_restores_without_the_wire_refusal(self):
        from prodagent.kernel.scheduler import Scheduler

        preset = self._preset()
        scheduler = Scheduler(initial_plan=preset)

        plan, states = Plan.from_state(
            self._incident_state(),
            plan_id="89515c25933a:1::audit_workflow",
            body_binder=scheduler._restore_binder,
        )

        assert plan.get_node("work").body is preset.get_node("work").body
        assert state_of(states, "plan").status is NodeStatus.COMPLETED
        assert state_of(states, "work").status is NodeStatus.PENDING
        assert plan.get_node("work").params == {"goal": "{{plan.output}}"}
