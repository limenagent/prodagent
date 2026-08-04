import pytest

from prodagent.resilience.reliability import ChainOptimizer, ReliabilityTier
from prodagent.runtime.plan.dag import Plan, PlanStep, StepStatus


def test_chain_optimizer_analyse_simple_chain():
    optimizer = ChainOptimizer()
    result = optimizer.analyse(n_steps=10, step_reliability=0.95)
    assert result.n_steps == 10
    assert result.step_reliability == 0.95
    assert result.serial_end_to_end == pytest.approx(0.95**10, abs=1e-3)


def test_chain_optimizer_analyse_critical_case():
    optimizer = ChainOptimizer()
    result = optimizer.analyse(n_steps=19, step_reliability=0.95)
    assert result.severity is ReliabilityTier.CRITICAL
    assert result.severity.description == "split this chain"
    assert result.serial_end_to_end < 0.5


def test_chain_optimizer_analyse_warning_case():
    optimizer = ChainOptimizer()
    result = optimizer.analyse(n_steps=7, step_reliability=0.95)
    assert result.severity is ReliabilityTier.WARNING
    assert result.severity.description == "consider splitting"
    assert 0.5 <= result.serial_end_to_end < 0.75


def test_chain_optimizer_analyse_ok_case():
    optimizer = ChainOptimizer()
    result = optimizer.analyse(n_steps=3, step_reliability=0.95)
    assert result.severity is ReliabilityTier.OK
    assert result.severity.description == "OK"
    assert result.serial_end_to_end >= 0.75


def test_chain_optimizer_analyse_plan_empty():
    optimizer = ChainOptimizer()
    plan = Plan()
    result = optimizer.analyse_plan(plan)
    assert result.total_steps == 0
    assert result.critical_path == []
    assert result.parallel_degree == 0.0
    assert result.base.serial_end_to_end == pytest.approx(1.0)


def test_chain_optimizer_analyse_plan_serial():
    optimizer = ChainOptimizer()
    steps = [
        PlanStep(
            step_id=f"step_{i}",
            action="test",
            params={},
            depends_on=[f"step_{i - 1}"] if i > 0 else [],
        )
        for i in range(5)
    ]
    plan = Plan()
    plan.add_steps(steps)
    result = optimizer.analyse_plan(plan)
    assert result.total_steps == 5
    assert len(result.critical_path) == 5
    assert result.parallel_degree == pytest.approx(1.0)


def test_chain_optimizer_analyse_plan_parallel():
    optimizer = ChainOptimizer()
    steps = [
        PlanStep(
            step_id=f"step_{i}",
            action="test",
            params={},
            depends_on=[],
        )
        for i in range(6)
    ]
    plan = Plan()
    plan.add_steps(steps)
    result = optimizer.analyse_plan(plan)
    assert result.total_steps == 6
    assert len(result.critical_path) == 1
    assert result.parallel_degree == pytest.approx(6.0)


def test_chain_optimizer_analyse_plan_mixed():
    optimizer = ChainOptimizer()
    steps = [
        PlanStep(step_id="step_0", action="root", params={}, depends_on=[]),
        PlanStep(step_id="step_1", action="branch1", params={}, depends_on=["step_0"]),
        PlanStep(step_id="step_2", action="branch2", params={}, depends_on=["step_0"]),
        PlanStep(step_id="step_3", action="branch3", params={}, depends_on=["step_0"]),
        PlanStep(step_id="step_4", action="leaf1", params={}, depends_on=["step_1"]),
    ]
    plan = Plan()
    plan.add_steps(steps)
    result = optimizer.analyse_plan(plan)
    assert result.total_steps == 5
    assert len(result.critical_path) == 3
    assert result.parallel_degree == pytest.approx(5 / 3, abs=0.1)


def test_chain_optimizer_analyse_plan_ignores_obsolete():
    optimizer = ChainOptimizer()
    steps = [
        PlanStep(step_id="step_0", action="root", params={}, depends_on=[]),
        PlanStep(
            step_id="step_1",
            action="obsolete",
            params={},
            depends_on=["step_0"],
            status=StepStatus.OBSOLETE,
        ),
        PlanStep(step_id="step_2", action="active", params={}, depends_on=["step_0"]),
    ]
    plan = Plan()
    plan.add_steps(steps)
    result = optimizer.analyse_plan(plan)
    assert result.total_steps == 2
    assert result.base.serial_end_to_end == pytest.approx(0.95**2)


def test_chain_optimizer_analyse_plan_critical_path():
    optimizer = ChainOptimizer()
    steps = [
        PlanStep(step_id="a", action="root", params={}, depends_on=[]),
        PlanStep(step_id="b", action="middle", params={}, depends_on=["a"]),
        PlanStep(step_id="c", action="branch", params={}, depends_on=["a"]),
        PlanStep(step_id="d", action="end", params={}, depends_on=["b", "c"]),
    ]
    plan = Plan()
    plan.add_steps(steps)
    result = optimizer.analyse_plan(plan)
    assert len(result.critical_path) == 3
    assert set(result.critical_path) == {"a", "b", "d"}


def test_chain_optimizer_custom_step_reliability():
    optimizer = ChainOptimizer()
    result = optimizer.analyse(n_steps=5, step_reliability=0.99)
    assert result.step_reliability == 0.99
    assert result.serial_end_to_end == pytest.approx(0.99**5, abs=1e-3)


def test_chain_optimizer_analyse_plan_custom_reliability():
    optimizer = ChainOptimizer()
    steps = [
        PlanStep(
            step_id=f"step_{i}",
            action="test",
            params={},
            depends_on=[f"step_{i - 1}"] if i > 0 else [],
        )
        for i in range(3)
    ]
    plan = Plan()
    plan.add_steps(steps)
    result = optimizer.analyse_plan(plan, step_reliability=0.9)
    assert result.base.step_reliability == 0.9
    assert result.base.serial_end_to_end == pytest.approx(0.9**3, abs=1e-3)


def test_chain_optimizer_analyse_zero_steps():
    optimizer = ChainOptimizer()
    result = optimizer.analyse(n_steps=0, step_reliability=0.95)
    assert result.n_steps == 0
    assert result.serial_end_to_end == pytest.approx(1.0)
    assert result.severity is ReliabilityTier.OK
