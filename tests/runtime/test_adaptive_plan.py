import pytest

from prodagent.kernel.graph import Node, Plan, fresh_states, state_of
from prodagent.kernel.graph_validator import default_validator
from prodagent.kernel.node_state import NodeRuntimeState
from prodagent.kernel.types import NodeStatus
from prodagent.kernel.units import ToolUnit


@pytest.fixture
def empty_plan():
    return Plan()


@pytest.fixture
def sample_plan(empty_plan):
    nodes = [
        Node(node_id="step_0", body=ToolUnit("root"), params={}, depends_on=[]),
        Node(node_id="step_1", body=ToolUnit("middle"), params={}, depends_on=["step_0"]),
        Node(node_id="step_2", body=ToolUnit("branch"), params={}, depends_on=["step_0"]),
        Node(node_id="step_3", body=ToolUnit("end"), params={}, depends_on=["step_1", "step_2"]),
    ]
    empty_plan.add_nodes(nodes)
    return empty_plan


@pytest.fixture
def states(sample_plan):
    return fresh_states(sample_plan)


def _completed(states, node_id, output=None):
    states[node_id].mark_running()
    states[node_id].mark_completed(output)


def _failed(states, node_id):
    states[node_id].mark_running()
    states[node_id].mark_failed("boom")


def test_node_creation():
    node = Node(
        node_id="test_node",
        body=ToolUnit("test_action"),
        params={"arg": "value"},
    )
    assert node.node_id == "test_node"
    assert node.action == "test_action"
    assert dict(node.params) == {"arg": "value"}
    assert list(node.depends_on) == []
    assert state_of({}, "test_node").status is NodeStatus.PENDING
    assert node.version_created == 1


def test_node_with_dependencies():
    node = Node(
        node_id="test_node",
        body=ToolUnit("test_action"),
        params={},
        depends_on=["dep1", "dep2"],
    )
    assert list(node.depends_on) == ["dep1", "dep2"]


def test_node_output_lives_on_state():
    state = NodeRuntimeState("test_node")
    state.mark_running()
    state.mark_completed({"result": "value"})
    assert state.output_ref == {"result": "value"}


def test_node_error_lives_on_state():
    state = NodeRuntimeState("test_node")
    state.mark_running()
    state.mark_failed("Something went wrong")
    assert state.error == "Something went wrong"


def test_plan_from_fixture(sample_plan):
    assert len(sample_plan.nodes) == 4
    assert sample_plan.version == 1


def test_execution_plan_init_with_id():
    plan_id = "test-plan-id"
    plan = Plan(plan_id=plan_id)
    assert plan.plan_id == plan_id


def test_execution_plan_init_generates_id():
    plan = Plan()
    assert plan.plan_id is not None
    assert len(plan.plan_id) > 0


def test_get_node_returns_node(sample_plan):
    node = sample_plan.get_node("step_0")
    assert node is not None
    assert node.node_id == "step_0"


def test_get_node_returns_none_for_nonexistent(sample_plan):
    assert sample_plan.get_node("nonexistent") is None


def test_ready_returns_multiple(sample_plan, states):
    _completed(states, "step_0")

    ready = sample_plan.ready(states)
    assert len(ready) == 2
    assert {n.node_id for n in ready} == {"step_1", "step_2"}


def test_ready_returns_root(empty_plan):
    nodes = [Node(node_id="step_0", body=ToolUnit("root"), params={}, depends_on=[])]
    empty_plan.add_nodes(nodes)
    ready = empty_plan.ready(fresh_states(empty_plan))
    assert len(ready) == 1
    assert ready[0].node_id == "step_0"


def test_ready_raises_on_missing_dependency(empty_plan):
    nodes = [Node(node_id="step_0", body=ToolUnit("root"), params={}, depends_on=["missing_dep"])]
    empty_plan.add_nodes(nodes)
    with pytest.raises(ValueError, match="unknown dependency"):
        empty_plan.ready({})


def test_mark_downstream_obsolete_preserves_failed_node(sample_plan, states):
    _failed(states, "step_1")

    obsolete = sample_plan.mark_downstream_obsolete("step_1", states)
    assert states["step_1"].status is NodeStatus.FAILED
    assert "step_1" not in obsolete


def test_mark_downstream_obsolete_marks_transitive_dependents(sample_plan, states):
    _failed(states, "step_1")

    obsolete = sample_plan.mark_downstream_obsolete("step_1", states)
    assert "step_1" not in obsolete
    assert "step_3" in obsolete


def test_mark_downstream_obsolete_preserves_completed(sample_plan, states):
    _completed(states, "step_1")
    _failed(states, "step_2")

    obsolete = sample_plan.mark_downstream_obsolete("step_2", states)
    assert "step_1" not in obsolete
    assert states["step_1"].status is NodeStatus.COMPLETED
    assert "step_3" in obsolete


def test_mark_downstream_obsolete_on_nonexistent_node(sample_plan, states):
    assert sample_plan.mark_downstream_obsolete("nonexistent", states) == []


def test_merge_adds_new_nodes(empty_plan):
    new_nodes = [Node(node_id="new_node", body=ToolUnit("new"), params={}, depends_on=[])]
    merged = empty_plan.merge(new_nodes, {})
    assert merged.get_node("new_node") is not None
    assert merged.version == 2


def test_merge_replaces_node_if_specified(sample_plan, states):
    new_nodes = [
        Node(
            node_id="new_node",
            body=ToolUnit("new"),
            params={},
            depends_on=[],
            replaces_node_id="step_1",
        )
    ]
    merged = sample_plan.merge(new_nodes, states)
    assert states["step_1"].status is NodeStatus.OBSOLETE
    assert merged.get_node("new_node") is not None


def test_merge_raises_on_missing_dependency(empty_plan):
    new_nodes = [Node(node_id="new_node", body=ToolUnit("new"), params={}, depends_on=["missing"])]
    with pytest.raises(ValueError, match="dependency .* not found"):
        empty_plan.merge(new_nodes, {})


def test_merge_raises_on_obsolete_dependency(sample_plan, states):
    states["step_1"].mark_obsolete()

    new_nodes = [Node(node_id="new_node", body=ToolUnit("new"), params={}, depends_on=["step_1"])]
    with pytest.raises(ValueError, match="dependency .* is OBSOLETE"):
        sample_plan.merge(new_nodes, states)


def test_merge_returns_new_version(sample_plan):
    original_version = sample_plan.version
    new_nodes = [Node(node_id="new_node", body=ToolUnit("new"), params={}, depends_on=[])]
    merged = sample_plan.merge(new_nodes, {})
    assert merged.version == original_version + 1
    assert sample_plan.version == original_version  # the old blueprint is untouched


def test_merge_sets_version_created(sample_plan):
    new_nodes = [Node(node_id="new_node", body=ToolUnit("new"), params={}, depends_on=[])]
    merged = sample_plan.merge(new_nodes, {})
    assert merged.get_node("new_node").version_created == 2


def test_assert_no_cycles_passes_on_dag(empty_plan):
    nodes = [
        Node(node_id="a", body=ToolUnit("a"), params={}, depends_on=[]),
        Node(node_id="b", body=ToolUnit("b"), params={}, depends_on=["a"]),
        Node(node_id="c", body=ToolUnit("c"), params={}, depends_on=["a", "b"]),
    ]
    empty_plan.add_nodes(nodes)


def test_assert_no_cycles_detects_cycle(empty_plan):
    nodes = [
        Node(node_id="a", body=ToolUnit("a"), params={}, depends_on=["c"]),
        Node(node_id="b", body=ToolUnit("b"), params={}, depends_on=["a"]),
        Node(node_id="c", body=ToolUnit("c"), params={}, depends_on=["b"]),
    ]
    with pytest.raises(ValueError, match="Cycle detected"):
        default_validator().validate_nodes(nodes)


def test_assert_no_cycles_in_cycle(empty_plan):
    nodes = [
        Node(node_id="a", body=ToolUnit("a"), params={}, depends_on=["b"]),
        Node(node_id="b", body=ToolUnit("b"), params={}, depends_on=["a"]),
    ]
    with pytest.raises(ValueError, match="Cycle detected"):
        default_validator().validate_nodes(nodes)


def test_is_complete_when_all_completed(sample_plan, states):
    for st in states.values():
        st.mark_running()
        st.mark_completed("out")
    assert sample_plan.is_complete(states)


def test_is_complete_when_mixed_with_obsolete(sample_plan, states):
    for i, st in enumerate(states.values()):
        if i % 2 == 0:
            st.mark_running()
            st.mark_completed("out")
        else:
            st.mark_obsolete()
    assert sample_plan.is_complete(states)


def test_is_complete_when_pending(sample_plan, states):
    assert not sample_plan.is_complete(states)


def test_is_complete_when_failed(sample_plan, states):
    _failed(states, "step_0")
    assert not sample_plan.is_complete(states)


def test_add_nodes_preserves_version_on_first_add(empty_plan):
    nodes = [Node(node_id="step_0", body=ToolUnit("root"), params={}, depends_on=[])]
    empty_plan.add_nodes(nodes)
    assert empty_plan.version == 1


def test_node_status_enum_values():
    assert NodeStatus.PENDING == "pending"
    assert NodeStatus.RUNNING == "running"
    assert NodeStatus.COMPLETED == "completed"
    assert NodeStatus.FAILED == "failed"
    assert NodeStatus.OBSOLETE == "obsolete"


def test_add_nodes_overwrites_existing_node(empty_plan):
    nodes = [Node(node_id="dup", body=ToolUnit("v1"), params={}, depends_on=[])]
    empty_plan.add_nodes(nodes)
    nodes2 = [Node(node_id="dup", body=ToolUnit("v2"), params={}, depends_on=[])]
    empty_plan.add_nodes(nodes2)
    assert empty_plan.get_node("dup").action == "v2"


def test_nodes_property_returns_mapping(sample_plan):
    nodes = sample_plan.nodes
    assert isinstance(nodes, dict)
    assert len(nodes) == 4


def test_nodes_property_is_copy(sample_plan):
    nodes1 = sample_plan.nodes
    nodes2 = sample_plan.nodes
    assert nodes1 is not nodes2


def test_ready_filters_by_status(sample_plan, states):
    _completed(states, "step_0")
    states["step_1"].mark_running()

    ready = sample_plan.ready(states)
    assert {n.node_id for n in ready} == {"step_2"}


def test_mark_downstream_obsolete_uses_bfs(sample_plan, states):
    sample_plan.add_nodes(
        [Node(node_id="step_4", body=ToolUnit("deep"), params={}, depends_on=["step_3"])]
    )
    states["step_4"] = NodeRuntimeState("step_4")

    _failed(states, "step_1")

    obsolete = sample_plan.mark_downstream_obsolete("step_1", states)
    assert set(obsolete) == {"step_3", "step_4"}


def test_replaces_node_id_in_node():
    node = Node(
        node_id="new_node",
        body=ToolUnit("new"),
        params={},
        depends_on=[],
        replaces_node_id="old_node",
    )
    assert node.replaces_node_id == "old_node"
