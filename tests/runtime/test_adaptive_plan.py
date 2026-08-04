import pytest

from prodagent.runtime.plan.dag import Plan, PlanStep, StepStatus


@pytest.fixture
def empty_plan():
    return Plan()


@pytest.fixture
def sample_plan(empty_plan):
    steps = [
        PlanStep(step_id="step_0", action="root", params={}, depends_on=[]),
        PlanStep(step_id="step_1", action="middle", params={}, depends_on=["step_0"]),
        PlanStep(step_id="step_2", action="branch", params={}, depends_on=["step_0"]),
        PlanStep(step_id="step_3", action="end", params={}, depends_on=["step_1", "step_2"]),
    ]
    empty_plan.add_steps(steps)
    return empty_plan


def test_plan_step_creation():
    step = PlanStep(
        step_id="test_step",
        action="test_action",
        params={"arg": "value"},
    )
    assert step.step_id == "test_step"
    assert step.action == "test_action"
    assert step.params == {"arg": "value"}
    assert step.depends_on == []
    assert step.status == StepStatus.PENDING
    assert step.version_created == 1


def test_plan_step_with_dependencies():
    step = PlanStep(
        step_id="test_step",
        action="test_action",
        params={},
        depends_on=["dep1", "dep2"],
    )
    assert step.depends_on == ["dep1", "dep2"]


def test_plan_step_output_ref():
    step = PlanStep(
        step_id="test_step",
        action="test_action",
        params={},
    )
    step.output_ref = {"result": "value"}
    assert step.output_ref == {"result": "value"}


def test_plan_step_error():
    step = PlanStep(
        step_id="test_step",
        action="test_action",
        params={},
    )
    step.error = "Something went wrong"
    assert step.error == "Something went wrong"


def test_plan_from_fixture(sample_plan):
    assert len(sample_plan.steps) == 4
    assert sample_plan.version == 1


def test_execution_plan_init_with_id():
    plan_id = "test-plan-id"
    plan = Plan(plan_id=plan_id)
    assert plan.plan_id == plan_id


def test_execution_plan_init_generates_id():
    plan = Plan()
    assert plan.plan_id is not None
    assert len(plan.plan_id) > 0


def test_get_step_returns_step(sample_plan):
    step = sample_plan.get_step("step_0")
    assert step is not None
    assert step.step_id == "step_0"


def test_get_step_returns_none_for_nonexistent(sample_plan):
    step = sample_plan.get_step("nonexistent")
    assert step is None


def test_get_parallel_ready_returns_multiple(sample_plan):
    step = sample_plan.get_step("step_0")
    step.status = StepStatus.COMPLETED

    ready_steps = sample_plan.get_parallel_ready()
    assert len(ready_steps) == 2
    step_ids = {s.step_id for s in ready_steps}
    assert step_ids == {"step_1", "step_2"}


def test_get_parallel_ready_returns_root(empty_plan):
    steps = [PlanStep(step_id="step_0", action="root", params={}, depends_on=[])]
    empty_plan.add_steps(steps)
    ready_steps = empty_plan.get_parallel_ready()
    assert len(ready_steps) == 1
    assert ready_steps[0].step_id == "step_0"


def test_all_deps_completed_raises_on_missing_dependency(empty_plan):
    steps = [PlanStep(step_id="step_0", action="root", params={}, depends_on=["missing_dep"])]
    empty_plan.add_steps(steps)
    with pytest.raises(ValueError, match="unknown dependency"):
        empty_plan.get_parallel_ready()


def test_mark_downstream_obsolete_preserves_failed_step(sample_plan):
    step = sample_plan.get_step("step_1")
    step.status = StepStatus.FAILED

    obsolete = sample_plan.mark_downstream_obsolete("step_1")
    assert step.status == StepStatus.FAILED
    assert "step_1" not in obsolete


def test_mark_downstream_obsolete_marks_transitive_dependents(sample_plan):
    step = sample_plan.get_step("step_1")
    step.status = StepStatus.FAILED

    obsolete = sample_plan.mark_downstream_obsolete("step_1")
    assert "step_1" not in obsolete
    assert "step_3" in obsolete


def test_mark_downstream_obsolete_preserves_completed_steps(sample_plan):
    step1 = sample_plan.get_step("step_1")
    step1.status = StepStatus.COMPLETED

    step2 = sample_plan.get_step("step_2")
    step2.status = StepStatus.FAILED

    obsolete = sample_plan.mark_downstream_obsolete("step_2")
    assert "step_1" not in obsolete
    assert step1.status == StepStatus.COMPLETED
    assert "step_3" in obsolete


def test_mark_downstream_obsolete_on_nonexistent_step(sample_plan):
    obsolete = sample_plan.mark_downstream_obsolete("nonexistent")
    assert obsolete == []


def test_merge_adds_new_steps(empty_plan):
    new_steps = [PlanStep(step_id="new_step", action="new", params={}, depends_on=[])]
    empty_plan.merge(new_steps)
    assert empty_plan.get_step("new_step") is not None
    assert empty_plan.version == 2


def test_merge_replaces_step_if_specified(sample_plan):
    new_steps = [
        PlanStep(
            step_id="new_step",
            action="new",
            params={},
            depends_on=[],
            replaces_step_id="step_1",
        )
    ]
    sample_plan.merge(new_steps)
    old_step = sample_plan.get_step("step_1")
    assert old_step.status == StepStatus.OBSOLETE
    assert sample_plan.get_step("new_step") is not None


def test_merge_raises_on_missing_dependency(empty_plan):
    new_steps = [PlanStep(step_id="new_step", action="new", params={}, depends_on=["missing"])]
    with pytest.raises(ValueError, match="dependency .* not found"):
        empty_plan.merge(new_steps)


def test_merge_raises_on_obsolete_dependency(sample_plan):
    step1 = sample_plan.get_step("step_1")
    step1.status = StepStatus.OBSOLETE

    new_steps = [PlanStep(step_id="new_step", action="new", params={}, depends_on=["step_1"])]
    with pytest.raises(ValueError, match="dependency .* is OBSOLETE"):
        sample_plan.merge(new_steps)


def test_merge_increments_version(sample_plan):
    original_version = sample_plan.version
    new_steps = [PlanStep(step_id="new_step", action="new", params={}, depends_on=[])]
    sample_plan.merge(new_steps)
    assert sample_plan.version == original_version + 1


def test_merge_sets_version_created(sample_plan):
    new_steps = [PlanStep(step_id="new_step", action="new", params={}, depends_on=[])]
    sample_plan.merge(new_steps)
    new_step = sample_plan.get_step("new_step")
    assert new_step.version_created == 2


def test_assert_no_cycles_passes_on_dag(empty_plan):
    steps = [
        PlanStep(step_id="a", action="a", params={}, depends_on=[]),
        PlanStep(step_id="b", action="b", params={}, depends_on=["a"]),
        PlanStep(step_id="c", action="c", params={}, depends_on=["a", "b"]),
    ]
    empty_plan.add_steps(steps)


def test_assert_no_cycles_detects_cycle(empty_plan):
    steps = [
        PlanStep(step_id="a", action="a", params={}, depends_on=["c"]),
        PlanStep(step_id="b", action="b", params={}, depends_on=["a"]),
        PlanStep(step_id="c", action="c", params={}, depends_on=["b"]),
    ]
    with pytest.raises(ValueError, match="Cycle detected"):
        empty_plan.add_steps(steps)


def test_assert_no_cycles_in_cycle(empty_plan):
    steps = [
        PlanStep(step_id="a", action="a", params={}, depends_on=["b"]),
        PlanStep(step_id="b", action="b", params={}, depends_on=["a"]),
    ]
    with pytest.raises(ValueError, match="Cycle detected"):
        empty_plan.add_steps(steps)


def test_is_complete_when_all_completed(sample_plan):
    for step in sample_plan.steps:
        step.status = StepStatus.COMPLETED
    assert sample_plan.is_complete()


def test_is_complete_when_mixed_with_obsolete(sample_plan):
    for i, step in enumerate(sample_plan.steps):
        if i % 2 == 0:
            step.status = StepStatus.COMPLETED
        else:
            step.status = StepStatus.OBSOLETE
    assert sample_plan.is_complete()


def test_is_complete_when_pending(sample_plan):
    assert not sample_plan.is_complete()


def test_is_complete_when_failed(sample_plan):
    step = sample_plan.get_step("step_0")
    step.status = StepStatus.FAILED
    assert not sample_plan.is_complete()


def test_add_steps_preserves_version_on_first_add(empty_plan):
    steps = [PlanStep(step_id="step_0", action="root", params={}, depends_on=[])]
    empty_plan.add_steps(steps)
    assert empty_plan.version == 1


def test_step_status_enum_values():
    assert StepStatus.PENDING == "pending"
    assert StepStatus.RUNNING == "running"
    assert StepStatus.COMPLETED == "completed"
    assert StepStatus.FAILED == "failed"
    assert StepStatus.OBSOLETE == "obsolete"


def test_add_steps_overwrites_existing_step(empty_plan):
    steps = [PlanStep(step_id="dup", action="v1", params={}, depends_on=[])]
    empty_plan.add_steps(steps)
    steps2 = [PlanStep(step_id="dup", action="v2", params={}, depends_on=[])]
    empty_plan.add_steps(steps2)
    step = empty_plan.get_step("dup")
    assert step.action == "v2"


def test_steps_property_returns_list(sample_plan):
    steps = sample_plan.steps
    assert isinstance(steps, list)
    assert len(steps) == 4


def test_steps_property_is_copy(sample_plan):
    steps1 = sample_plan.steps
    steps2 = sample_plan.steps
    assert steps1 is not steps2


def test_get_parallel_ready_filters_by_status(sample_plan):
    step = sample_plan.get_step("step_0")
    step.status = StepStatus.COMPLETED

    step1 = sample_plan.get_step("step_1")
    step1.status = StepStatus.RUNNING

    ready = sample_plan.get_parallel_ready()
    step_ids = {s.step_id for s in ready}
    assert step_ids == {"step_2"}


def test_mark_downstream_obsolete_uses_bfs(sample_plan):
    step4 = PlanStep(step_id="step_4", action="deep", params={}, depends_on=["step_3"])
    sample_plan.add_steps([step4])

    step1 = sample_plan.get_step("step_1")
    step1.status = StepStatus.FAILED

    obsolete = sample_plan.mark_downstream_obsolete("step_1")
    assert set(obsolete) == {"step_3", "step_4"}


def test_replaces_step_id_in_step():
    step = PlanStep(
        step_id="new_step",
        action="new",
        params={},
        depends_on=[],
        replaces_step_id="old_step",
    )
    assert step.replaces_step_id == "old_step"
