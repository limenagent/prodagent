from __future__ import annotations

import asyncio
import tempfile
from pathlib import Path

from prodagent import RunState
from prodagent.core.events import RunCompletedEvent
from prodagent.core.state import AgentRun
from prodagent.evaluation.evals.dataset import (
    EvalReport,
    ExampleResult,
    GoldenDataset,
    GoldenExample,
)
from prodagent.evaluation.evals.runner import EvalRunner, RegressionDetector, RegressionLevel
from prodagent.llm.fake import script


def _make_dataset(n: int = 2) -> GoldenDataset:
    ds = GoldenDataset("test-golden")
    for i in range(n):
        ds.add(
            GoldenExample(
                id=f"ex-{i + 1:03d}",
                task=f"Task {i + 1}",
                expected_output_contains=["done"],
                max_turns=5,
            )
        )
    return ds


def _make_report(pass_count: int, total: int, tag: str = "baseline") -> EvalReport:
    import time

    results = []
    for i in range(total):
        results.append(
            ExampleResult(
                example_id=f"ex-{i + 1:03d}",
                passed=(i < pass_count),
                turn_count=3,
                cost_usd=0.001,
                wall_seconds=0.1,
                tool_sequence=[],
                final_output="done" if i < pass_count else "",
                failure_reason=None if i < pass_count else "missing phrase",
            )
        )
    return EvalReport(
        dataset_name="test-golden",
        dataset_version="v1",
        tag=tag,
        created_at=time.time(),
        results=results,
    )


def test_evalrunner_runs_all_examples():
    calls: list[str] = []

    async def factory(example: GoldenExample) -> AgentRun:
        calls.append(example.id)
        run = AgentRun(run_id=example.id, task=example.task)
        run.state = RunState.COMPLETED
        run.final_output = "done"
        run.metrics.turn_count = 2
        return run

    ds = _make_dataset(3)
    runner = EvalRunner(factory, tag="smoke")
    report = asyncio.run(runner.run(ds))

    assert len(report.results) == 3
    assert set(calls) == {"ex-001", "ex-002", "ex-003"}


def test_evalrunner_pass_rate():
    async def factory(example: GoldenExample) -> AgentRun:
        run = AgentRun(run_id=example.id, task=example.task)
        run.state = RunState.COMPLETED
        run.final_output = "done"
        run.metrics.turn_count = 1
        return run

    ds = _make_dataset(2)
    runner = EvalRunner(factory, tag="t1")
    report = asyncio.run(runner.run(ds))

    assert report.pass_rate == 1.0
    assert report.total_cost_usd >= 0.0


def test_evalrunner_fail_when_output_missing():
    async def factory(example: GoldenExample) -> AgentRun:
        run = AgentRun(run_id=example.id, task=example.task)
        run.state = RunState.COMPLETED
        run.final_output = "nothing useful"
        run.metrics.turn_count = 1
        return run

    ds = _make_dataset(1)
    runner = EvalRunner(factory, tag="t2")
    report = asyncio.run(runner.run(ds))

    assert report.pass_rate == 0.0
    assert report.results[0].failure_reason is not None


def test_evalrunner_save_report():
    async def factory(example: GoldenExample) -> AgentRun:
        run = AgentRun(run_id=example.id, task=example.task)
        run.final_output = "done"
        run.metrics.turn_count = 1
        return run

    ds = _make_dataset(1)
    runner = EvalRunner(factory, tag="save-test")
    report = asyncio.run(runner.run(ds))

    with tempfile.TemporaryDirectory() as tmpdir:
        path = runner.save_report(report, tmpdir)
        assert Path(path).exists()
        loaded = runner.load_report(tmpdir, "test-golden", "save-test")
        assert loaded.tag == "save-test"
        assert len(loaded.results) == 1


def test_evalrunner_scripted_llm_through_full_stack():
    from prodagent.core.budget import HardBudget
    from prodagent.runtime.reactive import ReactiveLoop

    async def factory(example: GoldenExample) -> AgentRun:
        llm = script({"content": "done processing"})
        loop = ReactiveLoop(
            llm=llm,
            tool_executor=lambda call: asyncio.coroutine(lambda: {})(),
            budget=HardBudget(max_turns=3),
        )
        final_run: AgentRun | None = None
        async for event in loop.stream(example.task):
            if isinstance(event, RunCompletedEvent):
                final_run = event.run
        assert final_run is not None
        return final_run

    ds = _make_dataset(2)
    runner = EvalRunner(factory)
    report = asyncio.run(runner.run(ds))
    assert len(report.results) == 2


def _judge_json(safety: float, goal: float = 0.9, traj: float = 0.9, out: float = 0.9) -> str:
    import json

    return json.dumps(
        {
            "goal_achievement": {"score": goal, "reasoning": ""},
            "safety_compliance": {"score": safety, "reasoning": ""},
            "trajectory_quality": {"score": traj, "reasoning": ""},
            "output_quality": {"score": out, "reasoning": ""},
            "summary": "scripted",
        }
    )


def test_evalrunner_judge_scores_hard_gate_survivor():
    from prodagent.evaluation.evals.judge import LLMJudge

    async def factory(example: GoldenExample) -> AgentRun:
        run = AgentRun(run_id=example.id, task=example.task)
        run.state = RunState.COMPLETED
        run.final_output = "done"
        run.metrics.turn_count = 1
        return run

    judge = LLMJudge(llm=script({"content": _judge_json(safety=1.0)}), pass_threshold=0.7)
    ds = _make_dataset(1)
    runner = EvalRunner(factory, tag="judged", judge=judge)
    report = asyncio.run(runner.run(ds))

    r = report.results[0]
    assert r.passed
    assert r.judge_score is not None and r.judge_score > 0.7
    assert "judge" in r.metadata


def test_evalrunner_judge_blocks_on_safety_despite_hard_gate_pass():
    from prodagent.evaluation.evals.judge import LLMJudge

    async def factory(example: GoldenExample) -> AgentRun:
        run = AgentRun(run_id=example.id, task=example.task)
        run.state = RunState.COMPLETED
        run.final_output = "done"
        run.metrics.turn_count = 1
        return run

    judge = LLMJudge(llm=script({"content": _judge_json(safety=0.0)}), pass_threshold=0.7)
    ds = _make_dataset(1)
    runner = EvalRunner(factory, tag="judged", judge=judge)
    report = asyncio.run(runner.run(ds))

    r = report.results[0]
    assert not r.passed, "safety=0.0 is a blocking dimension — release must be vetoed"
    assert r.failure_reason is not None and "safety" in r.failure_reason.lower()


def test_evalrunner_judge_allows_imperfect_but_safe_blocking_dim():
    from prodagent.evaluation.evals.judge import LLMJudge

    async def factory(example: GoldenExample) -> AgentRun:
        run = AgentRun(run_id=example.id, task=example.task)
        run.state = RunState.COMPLETED
        run.final_output = "done"
        run.metrics.turn_count = 1
        return run

    judge = LLMJudge(llm=script({"content": _judge_json(safety=0.9)}), pass_threshold=0.7)
    ds = _make_dataset(1)
    runner = EvalRunner(factory, tag="judged", judge=judge)
    report = asyncio.run(runner.run(ds))

    r = report.results[0]
    assert r.passed, "safety=0.9 is imperfect but above the veto threshold — must release"
    assert r.judge_score is not None


def test_evalrunner_judge_blocking_threshold_is_configurable():
    from prodagent.evaluation.evals.judge import LLMJudge

    async def factory(example: GoldenExample) -> AgentRun:
        run = AgentRun(run_id=example.id, task=example.task)
        run.state = RunState.COMPLETED
        run.final_output = "done"
        run.metrics.turn_count = 1
        return run

    strict = LLMJudge(
        llm=script({"content": _judge_json(safety=0.8)}),
        pass_threshold=0.7,
        blocking_threshold=0.95,
    )
    ds = _make_dataset(1)
    runner = EvalRunner(factory, tag="judged", judge=strict)
    report = asyncio.run(runner.run(ds))

    r = report.results[0]
    assert not r.passed, "safety=0.8 < blocking_threshold=0.95 — must be vetoed"
    assert r.failure_reason is not None and "safety" in r.failure_reason.lower()


def test_evalrunner_judge_skipped_when_hard_gate_fails():
    from prodagent.evaluation.evals.judge import LLMJudge

    calls: list[int] = []

    class _CountingLLM:
        async def complete(self, *a, **k):  # noqa: ANN002, ANN003
            calls.append(1)
            from prodagent.llm.fake import script as _s

            return await _s({"content": _judge_json(safety=1.0)}).complete(*a, **k)

    async def factory(example: GoldenExample) -> AgentRun:
        run = AgentRun(run_id=example.id, task=example.task)
        run.state = RunState.COMPLETED
        run.final_output = "nothing useful"
        run.metrics.turn_count = 1
        return run

    judge = LLMJudge(llm=_CountingLLM())
    ds = _make_dataset(1)
    runner = EvalRunner(factory, tag="judged", judge=judge)
    report = asyncio.run(runner.run(ds))

    assert not report.results[0].passed
    assert calls == [], "judge must not be invoked on hard-gate failures"


def test_regression_detector_no_regression():
    baseline = _make_report(pass_count=5, total=5, tag="baseline")
    current = _make_report(pass_count=5, total=5, tag="current")
    det = RegressionDetector()
    result = det.compare(baseline, current)
    assert result.level == RegressionLevel.PASS
    assert not result.blocking


def test_regression_detector_detects_pass_rate_drop():
    baseline = _make_report(pass_count=10, total=10, tag="baseline")
    current = _make_report(pass_count=2, total=10, tag="current")
    det = RegressionDetector(warn_p=0.10, fail_p=0.05)
    result = det.compare(baseline, current)
    assert result.level in (RegressionLevel.WARN, RegressionLevel.FAIL)


def test_regression_detector_blocking_on_fail():
    baseline = _make_report(10, 10, "base")
    current = _make_report(0, 10, "cur")
    det = RegressionDetector(warn_p=0.10, fail_p=0.05)
    result = det.compare(baseline, current)
    assert result.blocking is (result.level == RegressionLevel.FAIL)


def test_regression_summary_string():
    baseline = _make_report(5, 5, "v1")
    current = _make_report(5, 5, "v2")
    det = RegressionDetector()
    result = det.compare(baseline, current)
    summary = result.summary()
    assert "v1" in summary
    assert "v2" in summary
    assert "pass_rate" in summary


def test_regression_detector_empty_reports_no_crash():
    import time

    baseline = EvalReport(dataset_name="d", dataset_version="v1", tag="b", created_at=time.time())
    current = EvalReport(dataset_name="d", dataset_version="v1", tag="c", created_at=time.time())
    det = RegressionDetector()
    result = det.compare(baseline, current)
    assert result.level == RegressionLevel.PASS


def _make_report_with_cost(
    costs: list[float], turns: list[int], *, tag: str = "baseline", pass_count: int | None = None
) -> EvalReport:
    import time

    total = len(costs)
    pc = pass_count if pass_count is not None else total
    results = []
    for i in range(total):
        results.append(
            ExampleResult(
                example_id=f"ex-{i + 1:03d}",
                passed=i < pc,
                turn_count=turns[i],
                cost_usd=costs[i],
                wall_seconds=0.1,
                tool_sequence=[],
                final_output="done" if i < pc else "",
                failure_reason=None if i < pc else "fail",
            )
        )
    return EvalReport(
        dataset_name="test-golden",
        dataset_version="v1",
        tag=tag,
        created_at=time.time(),
        results=results,
    )


def test_regression_detector_detects_cost_increase():
    baseline = _make_report_with_cost(
        [0.009, 0.010, 0.011, 0.010, 0.009, 0.011, 0.010, 0.009] * 5, [3] * 40, tag="baseline"
    )
    current = _make_report_with_cost(
        [0.049, 0.050, 0.051, 0.050, 0.049, 0.051, 0.050, 0.049] * 5, [3] * 40, tag="current"
    )
    det = RegressionDetector(warn_p=0.10, fail_p=0.05)
    result = det.compare(baseline, current)
    cost_cmp = next(c for c in result.comparisons if c.metric == "cost_usd")
    assert cost_cmp.level in (RegressionLevel.WARN, RegressionLevel.FAIL), (
        f"cost regression not detected: p={cost_cmp.p_value:.4f}, level={cost_cmp.level}"
    )


def test_regression_detector_detects_turn_count_increase():
    baseline = _make_report_with_cost([0.01] * 40, [3, 4, 3, 3, 4, 3, 3, 4] * 5, tag="baseline")
    current = _make_report_with_cost([0.01] * 40, [8, 9, 8, 8, 9, 8, 8, 9] * 5, tag="current")
    det = RegressionDetector(warn_p=0.10, fail_p=0.05)
    result = det.compare(baseline, current)
    turn_cmp = next(c for c in result.comparisons if c.metric == "turn_count")
    assert turn_cmp.level in (RegressionLevel.WARN, RegressionLevel.FAIL), (
        f"turn_count regression not detected: p={turn_cmp.p_value:.4f}, level={turn_cmp.level}"
    )


def test_judge_parses_markdown_fenced_json():
    from prodagent.evaluation.evals.judge import LLMJudge

    fenced = "```json\n" + _judge_json(safety=0.9) + "\n```"
    judge = LLMJudge(llm=script({"content": fenced}), pass_threshold=0.7)
    run = AgentRun(run_id="r1", task="t")
    run.state = RunState.COMPLETED
    run.final_output = "done"
    verdict = asyncio.run(judge.evaluate(run))
    assert verdict.overall_score > 0.7
    assert not verdict.blocking_failures


def test_judge_retries_on_empty_content_then_succeeds():
    from prodagent.evaluation.evals.judge import LLMJudge

    class _FlakyLLM:
        def __init__(self) -> None:
            self.calls = 0

        async def complete(self, *a, **k):  # noqa: ANN002, ANN003
            self.calls += 1
            content = "" if self.calls == 1 else _judge_json(safety=1.0)
            return await script({"content": content}).complete(*a, **k)

    flaky = _FlakyLLM()
    judge = LLMJudge(llm=flaky, pass_threshold=0.7, max_attempts=3)
    run = AgentRun(run_id="r1", task="t")
    run.state = RunState.COMPLETED
    run.final_output = "done"
    verdict = asyncio.run(judge.evaluate(run))
    assert flaky.calls == 2, "should retry once past the empty response"
    assert verdict.overall_score > 0.7


def test_judge_neutral_verdict_after_exhausting_retries():
    from prodagent.evaluation.evals.judge import LLMJudge

    class _AlwaysEmpty:
        def __init__(self) -> None:
            self.calls = 0

        async def complete(self, *a, **k):  # noqa: ANN002, ANN003
            self.calls += 1
            return await script({"content": ""}).complete(*a, **k)

    empty = _AlwaysEmpty()
    judge = LLMJudge(llm=empty, max_attempts=3)
    run = AgentRun(run_id="r1", task="t")
    run.state = RunState.COMPLETED
    verdict = asyncio.run(judge.evaluate(run))
    assert empty.calls == 3, "must exhaust all attempts"
    assert verdict.overall_score == 0.0
    assert not verdict.passed


def test_judge_error_does_not_flip_passed_to_false():
    from prodagent.evaluation.evals.judge import LLMJudge

    class _AlwaysEmpty:
        async def complete(self, *a, **k):  # noqa: ANN002, ANN003
            return await script({"content": ""}).complete(*a, **k)

    async def factory(example: GoldenExample) -> AgentRun:
        run = AgentRun(run_id=example.id, task=example.task)
        run.state = RunState.COMPLETED
        run.final_output = "done"
        run.metrics.turn_count = 1
        return run

    judge = LLMJudge(llm=_AlwaysEmpty(), max_attempts=2)
    ds = _make_dataset(1)
    runner = EvalRunner(factory, tag="judge-error", judge=judge)
    report = asyncio.run(runner.run(ds))

    r = report.results[0]
    assert r.passed, "judge_error must not flip passed to False"
    assert r.metadata.get("judge_error") is True
    assert "judge_error_reason" in r.metadata
    assert r.judge_score == 0.0


def test_judge_error_verdict_carries_judge_error_flag():
    from prodagent.evaluation.evals.judge import LLMJudge

    class _AlwaysEmpty:
        async def complete(self, *a, **k):  # noqa: ANN002, ANN003
            return await script({"content": ""}).complete(*a, **k)

    judge = LLMJudge(llm=_AlwaysEmpty(), max_attempts=2)
    run = AgentRun(run_id="r1", task="t")
    run.state = RunState.COMPLETED
    run.final_output = "done"
    verdict = asyncio.run(judge.evaluate(run))
    assert verdict.judge_error is True
    assert not verdict.passed


def test_judge_real_rejection_still_vetos():
    from prodagent.evaluation.evals.judge import LLMJudge

    async def factory(example: GoldenExample) -> AgentRun:
        run = AgentRun(run_id=example.id, task=example.task)
        run.state = RunState.COMPLETED
        run.final_output = "done"
        run.metrics.turn_count = 1
        return run

    judge = LLMJudge(llm=script({"content": _judge_json(safety=0.0)}), pass_threshold=0.7)
    ds = _make_dataset(1)
    runner = EvalRunner(factory, tag="real-reject", judge=judge)
    report = asyncio.run(runner.run(ds))

    r = report.results[0]
    assert not r.passed, "real judge rejection must still veto"
    assert not r.metadata.get("judge_error"), "real rejection is not a judge_error"
