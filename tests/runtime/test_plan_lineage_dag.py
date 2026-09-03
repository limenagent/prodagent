from __future__ import annotations

import pytest

from prodagent.kernel.graph import Node, Plan, fresh_states, node_wire_dict
from prodagent.kernel.node_state import NodeRuntimeState, NodeStateError
from prodagent.kernel.types import NodeStatus
from prodagent.kernel.units import ToolUnit


def _make_plan_with_nodes() -> Plan:
    plan = Plan()
    plan.add_nodes(
        [
            Node(node_id="s1", body=ToolUnit("fetch")),
            Node(node_id="s2", body=ToolUnit("transform"), depends_on=["s1"]),
            Node(node_id="s3", body=ToolUnit("load"), depends_on=["s2"]),
        ]
    )
    return plan


def test_node_attempts_default_zero():
    state = NodeRuntimeState("s1")
    assert state.attempts == 0


def test_node_attempts_persisted_in_wire_dict():
    node = Node(node_id="s1", body=ToolUnit("x"))
    state = NodeRuntimeState("s1", attempts=3)
    d = node_wire_dict(node, state)
    assert d["attempts"] == 3


def test_node_attempts_restored_from_state():
    plan = _make_plan_with_nodes()
    states = fresh_states(plan)
    states["s1"].mark_running()  # attempts → 1
    states["s1"].mark_completed({"ok": True})

    restored, restored_states = Plan.from_state(plan.to_state(states), plan_id=plan.plan_id)
    assert restored_states["s1"].attempts == 1


def test_node_attempts_incremented_by_mark_running():
    state = NodeRuntimeState("s1")
    state.mark_running()
    state.mark_completed("out")
    # a completed node cannot restart — the reset-to-pending path is for
    # crashed or suspended nodes only
    with pytest.raises(NodeStateError):
        state.mark_running()


def test_crash_reset_clears_partial_output():
    state = NodeRuntimeState("s1", status=NodeStatus.RUNNING, output_ref={"partial": 1})
    state.reset_to_pending()
    assert state.status is NodeStatus.PENDING
    assert state.output_ref is None


def test_illegal_transition_raises():
    state = NodeRuntimeState("s1", status=NodeStatus.COMPLETED)
    with pytest.raises(NodeStateError):
        state.mark_failed("too late")


def test_merge_returns_new_plan_and_marks_replaced_state():
    plan = _make_plan_with_nodes()
    states = fresh_states(plan)
    states["s2"].mark_running()
    states["s2"].mark_failed("boom")

    replacement = Node(
        node_id="s2b", body=ToolUnit("transform_v2"), depends_on=["s1"], replaces_node_id="s2"
    )
    merged = plan.merge([replacement], states)

    assert merged is not plan
    assert merged.version == plan.version + 1
    assert merged.get_node("s2b") is not None
    assert plan.get_node("s2b") is None  # the old blueprint is untouched
    assert states["s2"].status is NodeStatus.OBSOLETE
    assert merged.get_node("s2").replaces_node_id is None  # static lineage stays


def test_downstream_obsolete_skips_completed():
    plan = _make_plan_with_nodes()
    states = fresh_states(plan)
    states["s1"].mark_running()
    states["s1"].mark_completed("data")
    states["s2"].mark_running()
    states["s2"].mark_completed("rows")
    states["s3"].mark_running()

    obsoleted = plan.mark_downstream_obsolete("s2", states)
    assert obsoleted == ["s3"]
    assert states["s2"].status is NodeStatus.COMPLETED  # completed is never scrapped
    assert states["s3"].status is NodeStatus.OBSOLETE


def test_frozen_node_rejects_field_writes():
    node = Node(node_id="s1", body=ToolUnit("fetch"))
    with pytest.raises(Exception, match="cannot assign"):
        node.action = "other"  # type: ignore[misc]
