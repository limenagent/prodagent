from __future__ import annotations

from dataclasses import replace

import pytest

from prodagent.kernel.bodies import ToolBody
from prodagent.kernel.graph import Node, Plan
from prodagent.kernel.node_state import NodeRuntimeState
from prodagent.kernel.types import NodeStatus


def _make_plan_with_completed_dep(
    output_ref: object, params: dict | None = None
) -> tuple[Plan, Node, dict[str, NodeRuntimeState]]:
    step_1 = Node(node_id="step_1", body=ToolBody("tool_a"), params={})
    step_2 = Node(
        node_id="step_2",
        body=ToolBody("tool_b"),
        depends_on=["step_1"],
        params=params if params is not None else {"x": "{{step_1.output.val}}"},
    )
    plan = Plan()
    plan.add_nodes([step_1, step_2])
    states = {
        "step_1": NodeRuntimeState("step_1", status=NodeStatus.COMPLETED, output_ref=output_ref),
        "step_2": NodeRuntimeState("step_2"),
    }
    return plan, step_2, states


class TestResolveParams:
    def test_resolves_dict_key(self):
        plan, step_2, states = _make_plan_with_completed_dep({"val": "INC-42"})
        assert plan.resolve_params(step_2, states)["x"] == "INC-42"

    def test_output_prefix_is_optional(self):
        plan, step_2, states = _make_plan_with_completed_dep({"val": 7}, {"x": "{{step_1.val}}"})
        assert plan.resolve_params(step_2, states)["x"] == 7

    def test_single_template_preserves_native_type(self):
        plan, step_2, states = _make_plan_with_completed_dep({"val": 42})
        assert plan.resolve_params(step_2, states)["x"] == 42

    def test_template_embedded_in_text_is_stringified(self):
        plan, step_2, states = _make_plan_with_completed_dep(
            {"val": "INC-42"}, {"x": "ticket: {{step_1.output.val}} done"}
        )
        assert plan.resolve_params(step_2, states)["x"] == "ticket: INC-42 done"

    def test_resolves_nested_in_dict_and_list(self):
        plan, step_2, states = _make_plan_with_completed_dep(
            {"val": "X"},
            {
                "a": "{{step_1.output.val}}",
                "nested": {"b": "{{step_1.output.val}}"},
                "items": ["{{step_1.output.val}}"],
            },
        )
        resolved = plan.resolve_params(step_2, states)
        assert resolved["a"] == "X"
        assert resolved["nested"]["b"] == "X"
        assert resolved["items"][0] == "X"

    def test_missing_dict_key_raises(self):
        plan, step_2, states = _make_plan_with_completed_dep(
            {"other": "x"}, {"x": "{{step_1.output.nonexistent}}"}
        )
        with pytest.raises(ValueError) as exc_info:
            plan.resolve_params(step_2, states)
        assert "nonexistent" in str(exc_info.value)

    def test_references_nonexistent_step_raises(self):
        plan, step_2, states = _make_plan_with_completed_dep(
            {"val": 1}, {"x": "{{no_such_step.output.val}}"}
        )
        with pytest.raises(ValueError) as exc_info:
            plan.resolve_params(step_2, states)
        assert "no_such_step" in str(exc_info.value)

    def test_references_uncompleted_step_raises(self):
        step_1 = Node(node_id="step_1", body=ToolBody("tool_a"), params={})
        step_2 = Node(
            node_id="step_2",
            body=ToolBody("tool_b"),
            depends_on=["step_1"],
            params={"x": "{{step_1.output.val}}"},
        )
        plan = Plan()
        plan.add_nodes([step_1, step_2])
        with pytest.raises(ValueError) as exc_info:
            plan.resolve_params(step_2, {})
        assert "step_1" in str(exc_info.value)
        assert "not COMPLETED" in str(exc_info.value) or "pending" in str(exc_info.value).lower()

    def test_multi_level_path_rejected(self):
        plan, step_2, states = _make_plan_with_completed_dep(
            {"val": {"nested": 1}}, {"x": "{{step_1.output.val.nested}}"}
        )
        with pytest.raises(ValueError, match="single-key"):
            plan.resolve_params(step_2, states)

    def test_array_index_template_raises_not_passthrough(self):
        plan, step_2, states = _make_plan_with_completed_dep(
            {"results": [{"url": "https://x"}]}, {"url": "{{step_1.output.results[0].url}}"}
        )
        with pytest.raises(ValueError) as exc_info:
            plan.resolve_params(step_2, states)
        msg = str(exc_info.value)
        assert "unsupported template syntax" in msg
        assert "results[0].url" in msg

    def test_plain_string_without_template_passthrough(self):
        plan, step_2, states = _make_plan_with_completed_dep(
            {"val": "X"}, {"url": "https://example.com/page"}
        )
        assert plan.resolve_params(step_2, states)["url"] == "https://example.com/page"

    def test_node_params_are_frozen(self):
        _, step_2, _ = _make_plan_with_completed_dep({"val": "X"})
        with pytest.raises(Exception, match="cannot assign"):
            step_2.params = {"x": "1"}  # type: ignore[misc]

    def test_replacing_params_yields_a_new_node(self):
        _, step_2, _ = _make_plan_with_completed_dep({"val": "X"})
        twin = replace(step_2, params={"x": "1"})
        assert twin.params == {"x": "1"}
        assert step_2.params["x"] == "{{step_1.output.val}}"
