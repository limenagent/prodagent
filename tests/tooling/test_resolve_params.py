from __future__ import annotations

import pytest

from prodagent.plan.dag import Plan, PlanStep, StepStatus


def _make_plan_with_completed_dep(output_ref: object) -> tuple[Plan, PlanStep]:
    step_1 = PlanStep(step_id="step_1", action="tool_a", params={})
    step_1.status = StepStatus.COMPLETED
    step_1.output_ref = output_ref
    step_2 = PlanStep(
        step_id="step_2",
        action="tool_b",
        depends_on=["step_1"],
        params={"x": "{{step_1.output.val}}"},
    )
    plan = Plan()
    plan.add_steps([step_1, step_2])
    return plan, step_2


class TestResolveParams:
    def test_resolves_dict_key(self):
        plan, step_2 = _make_plan_with_completed_dep({"val": "INC-42"})
        assert plan.resolve_params(step_2)["x"] == "INC-42"

    def test_output_prefix_is_optional(self):
        plan, step_2 = _make_plan_with_completed_dep({"val": 7})
        step_2.params = {"x": "{{step_1.val}}"}
        assert plan.resolve_params(step_2)["x"] == 7

    def test_single_template_preserves_native_type(self):
        plan, step_2 = _make_plan_with_completed_dep({"val": 42})
        step_2.params = {"x": "{{step_1.output.val}}"}
        assert plan.resolve_params(step_2)["x"] == 42

    def test_template_embedded_in_text_is_stringified(self):
        plan, step_2 = _make_plan_with_completed_dep({"val": "INC-42"})
        step_2.params = {"x": "ticket: {{step_1.output.val}} done"}
        assert plan.resolve_params(step_2)["x"] == "ticket: INC-42 done"

    def test_resolves_nested_in_dict_and_list(self):
        plan, step_2 = _make_plan_with_completed_dep({"val": "X"})
        step_2.params = {
            "a": "{{step_1.output.val}}",
            "nested": {"b": "{{step_1.output.val}}"},
            "items": ["{{step_1.output.val}}"],
        }
        resolved = plan.resolve_params(step_2)
        assert resolved["a"] == "X"
        assert resolved["nested"]["b"] == "X"
        assert resolved["items"][0] == "X"

    def test_missing_dict_key_raises(self):
        plan, step_2 = _make_plan_with_completed_dep({"other": "x"})
        step_2.params = {"x": "{{step_1.output.nonexistent}}"}
        with pytest.raises(ValueError) as exc_info:
            plan.resolve_params(step_2)
        assert "nonexistent" in str(exc_info.value)

    def test_references_nonexistent_step_raises(self):
        plan, step_2 = _make_plan_with_completed_dep({"val": 1})
        step_2.params = {"x": "{{no_such_step.output.val}}"}
        with pytest.raises(ValueError) as exc_info:
            plan.resolve_params(step_2)
        assert "no_such_step" in str(exc_info.value)

    def test_references_uncompleted_step_raises(self):
        step_1 = PlanStep(step_id="step_1", action="tool_a", params={})
        step_2 = PlanStep(
            step_id="step_2",
            action="tool_b",
            depends_on=["step_1"],
            params={"x": "{{step_1.output.val}}"},
        )
        plan = Plan()
        plan.add_steps([step_1, step_2])
        with pytest.raises(ValueError) as exc_info:
            plan.resolve_params(step_2)
        assert "step_1" in str(exc_info.value)
        assert "not COMPLETED" in str(exc_info.value) or "pending" in str(exc_info.value).lower()

    def test_multi_level_path_rejected(self):
        plan, step_2 = _make_plan_with_completed_dep({"val": {"nested": 1}})
        step_2.params = {"x": "{{step_1.output.val.nested}}"}
        with pytest.raises(ValueError, match="single-key"):
            plan.resolve_params(step_2)

    def test_array_index_template_raises_not_passthrough(self):
        plan, step_2 = _make_plan_with_completed_dep({"results": [{"url": "https://x"}]})
        step_2.params = {"url": "{{step_1.output.results[0].url}}"}
        with pytest.raises(ValueError) as exc_info:
            plan.resolve_params(step_2)
        msg = str(exc_info.value)
        assert "unsupported template syntax" in msg
        assert "results[0].url" in msg

    def test_plain_string_without_template_passthrough(self):
        plan, step_2 = _make_plan_with_completed_dep({"val": "X"})
        step_2.params = {"url": "https://example.com/page"}
        assert plan.resolve_params(step_2)["url"] == "https://example.com/page"
