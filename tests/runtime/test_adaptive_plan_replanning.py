import pytest

from prodagent.kernel.bodies.base import ToolBody
from prodagent.kernel.types import NodeStatus
from prodagent.plan.dag import Node, Plan, fresh_states, state_of


def _node(sid: str, depends_on: list[str] | None = None, **kw) -> Node:
    return Node(node_id=sid, body=ToolBody(sid), depends_on=depends_on or [], **kw)


def _states_for(
    plan: Plan, completed: frozenset[str] = frozenset(), failed: frozenset[str] = frozenset()
) -> dict:
    states = fresh_states(plan)
    for sid in completed:
        states[sid].mark_running()
        states[sid].mark_completed("out")
    for sid in failed:
        states[sid].mark_running()
        states[sid].mark_failed("boom")
    return states


class TestMergeIntraBatchDependencies:
    def test_chain_of_new_nodes_accepted(self):
        plan = Plan()
        plan.add_nodes([_node("s1")])
        states = _states_for(plan, failed={"s1"})
        plan.mark_downstream_obsolete("s1", states)

        merged = plan.merge(
            [
                _node("s1_prime", replaces_node_id="s1"),
                _node("s2_prime", depends_on=["s1_prime"]),
            ],
            states,
        )

        assert merged.get_node("s2_prime") is not None
        assert state_of(states, "s1_prime").status is NodeStatus.PENDING
        assert state_of(states, "s2_prime").status is NodeStatus.PENDING

    def test_diamond_of_new_nodes_accepted(self):
        plan = Plan()
        plan.add_nodes([_node("root")])
        states = _states_for(plan, failed={"root"})
        plan.mark_downstream_obsolete("root", states)

        merged = plan.merge(
            [
                _node("root_prime", replaces_node_id="root"),
                _node("left_prime", depends_on=["root_prime"]),
                _node("right_prime", depends_on=["root_prime"]),
                _node("join_prime", depends_on=["left_prime", "right_prime"]),
            ],
            states,
        )

        assert merged.get_node("join_prime") is not None
        assert state_of(states, "join_prime").status is NodeStatus.PENDING

    def test_missing_external_dep_still_raises(self):
        plan = Plan()
        plan.add_nodes([_node("s1")])

        with pytest.raises(ValueError, match="not found"):
            plan.merge([_node("new_node", depends_on=["ghost_id"])], {})


class TestMarkDownstreamObsolete:
    def test_pending_past_completed_is_obsoleted(self):
        plan = Plan()
        plan.add_nodes(
            [
                _node("s1"),
                _node("s2", depends_on=["s1"]),
                _node("s3", depends_on=["s2"]),
            ]
        )
        states = _states_for(plan, completed={"s2"}, failed={"s1"})

        obsoleted = plan.mark_downstream_obsolete("s1", states)

        assert states["s1"].status is NodeStatus.FAILED, (
            "Failed node must retain FAILED status, not be overwritten to OBSOLETE"
        )
        assert states["s2"].status is NodeStatus.COMPLETED, (
            "COMPLETED node must not be downgraded to OBSOLETE"
        )
        assert states["s3"].status is NodeStatus.OBSOLETE, (
            "Transitive downstream past a COMPLETED node must be OBSOLETE"
        )

        assert "s1" not in obsoleted
        assert "s2" not in obsoleted
        assert "s3" in obsoleted

    def test_deep_chain_past_completed(self):
        plan = Plan()
        plan.add_nodes(
            [
                _node("root"),
                _node("A", depends_on=["root"]),
                _node("B", depends_on=["A"]),
                _node("C", depends_on=["B"]),
                _node("D", depends_on=["C"]),
            ]
        )
        states = _states_for(plan, completed={"A", "B"}, failed={"root"})

        plan.mark_downstream_obsolete("root", states)

        assert states["A"].status is NodeStatus.COMPLETED
        assert states["B"].status is NodeStatus.COMPLETED
        assert states["C"].status is NodeStatus.OBSOLETE
        assert states["D"].status is NodeStatus.OBSOLETE

    def test_no_downstream_completed_only(self):
        plan = Plan()
        plan.add_nodes(
            [
                _node("s1"),
                _node("s2", depends_on=["s1"]),
            ]
        )
        states = _states_for(plan, completed={"s2"}, failed={"s1"})

        obsoleted = plan.mark_downstream_obsolete("s1", states)

        assert states["s2"].status is NodeStatus.COMPLETED
        assert "s2" not in obsoleted
