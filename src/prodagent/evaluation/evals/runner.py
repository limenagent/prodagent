"""EvalRunner and RegressionDetector — run golden datasets and detect regressions."""

from __future__ import annotations

import asyncio
import json
import logging
import math
import statistics
import time
from collections.abc import Callable, Coroutine
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import TYPE_CHECKING, Any

from prodagent.core.state.run import AgentRun
from prodagent.evaluation.evals.dataset import (
    EvalReport,
    ExampleResult,
    GoldenDataset,
    GoldenExample,
)

if TYPE_CHECKING:
    from prodagent.evaluation.evals.judge import LLMJudge

logger = logging.getLogger(__name__)

AgentFactory = Callable[[GoldenExample], Coroutine[Any, Any, AgentRun]]


def _evaluate_example(example: GoldenExample, run: AgentRun) -> ExampleResult:
    """Derive pass/fail from an AgentRun against a GoldenExample's assertions."""
    tool_sequence = [tc.name for tc in run.tool_history]
    output = (run.final_output or "").lower()
    failure_reason: str | None = None
    metadata: dict[str, Any] = {}

    for forbidden in example.forbidden_tools:
        if forbidden in tool_sequence:
            failure_reason = f"Forbidden tool called: {forbidden!r}"
            break

    if failure_reason is None:
        for phrase in example.expected_output_contains:
            if phrase.lower() not in output:
                failure_reason = f"Output missing required phrase: {phrase!r}"
                break

    if (
        failure_reason is None
        and example.expected_tool_sequence
        and not _is_subsequence(example.expected_tool_sequence, tool_sequence)
    ):
        failure_reason = (
            f"Expected tool subsequence {example.expected_tool_sequence} "
            f"not found in actual {tool_sequence}"
        )

    # Drift is observability, not a gate — runs whenever a golden sequence is provided.
    if example.expected_tool_sequence:
        from prodagent.resilience.observability.drift import DriftDetector

        report = DriftDetector().compare(example.expected_tool_sequence, tool_sequence)
        if report:
            metadata["drift"] = sorted(report.kinds)
            logger.info(
                "DriftDetector[%s]: %s",
                example.id,
                "; ".join(d.detail for d in report.drifts),
            )

    if (
        failure_reason is None
        and example.max_turns is not None
        and run.turn_count > example.max_turns
    ):
        failure_reason = f"Exceeded max_turns: {run.turn_count} > {example.max_turns}"

    return ExampleResult(
        example_id=example.id,
        passed=failure_reason is None,
        turn_count=run.turn_count,
        cost_usd=run.cost_usd,
        wall_seconds=run.elapsed_seconds(),
        tool_sequence=tool_sequence,
        final_output=run.final_output or "",
        failure_reason=failure_reason,
        metadata=metadata,
    )


def _is_subsequence(needle: list[str], haystack: list[str]) -> bool:
    """Return True if needle is a subsequence (in order) of haystack."""
    it = iter(haystack)
    return all(item in it for item in needle)


class EvalRunner:
    """Runs a GoldenDataset through an agent and produces an EvalReport."""

    def __init__(
        self,
        agent_factory: AgentFactory,
        *,
        max_concurrency: int = 1,
        tag: str = "eval",
        model: str = "",
        judge: LLMJudge | None = None,
    ) -> None:
        self._factory = agent_factory
        self._concurrency = max(1, max_concurrency)
        self._tag = tag
        self._model = model
        self._judge = judge

    async def run(
        self,
        dataset: GoldenDataset,
        *,
        tags: list[str] | None = None,
    ) -> EvalReport:
        """Evaluate all examples in *dataset* and return an EvalReport."""
        examples = dataset.all(tags=tags)
        if not examples:
            logger.warning("EvalRunner: no examples to run (tags=%s)", tags)
            return EvalReport(
                dataset_name=dataset.name,
                dataset_version=dataset.version,
                tag=self._tag,
                created_at=time.time(),
                model=self._model,
            )

        logger.info(
            "EvalRunner: starting %d examples (concurrency=%d, tag=%r)",
            len(examples),
            self._concurrency,
            self._tag,
        )

        semaphore = asyncio.Semaphore(self._concurrency)
        results: list[ExampleResult] = []

        async def _run_one(example: GoldenExample) -> ExampleResult:
            async with semaphore:
                run: AgentRun | None = None
                try:
                    run = await self._factory(example)
                    result = _evaluate_example(example, run)
                    if self._judge is not None and result.passed:
                        result = await self._apply_judge(example, run, result)
                except Exception as exc:
                    logger.error("EvalRunner: example %r raised: %s", example.id, exc)
                    # Preserve partial cost/turns — zeroing masks cost regressions in failing examples.
                    result = ExampleResult(
                        example_id=example.id,
                        passed=False,
                        turn_count=run.turn_count if run is not None else 0,
                        cost_usd=run.cost_usd if run is not None else 0.0,
                        wall_seconds=run.elapsed_seconds() if run is not None else 0.0,
                        tool_sequence=[tc.name for tc in run.tool_history]
                        if run is not None
                        else [],
                        final_output=(run.final_output or "") if run is not None else "",
                        failure_reason=f"Unhandled exception: {exc}",
                    )
                status = "PASS" if result.passed else "FAIL"
                logger.info(
                    "  [%s] %s (turns=%d cost=$%.4f%s%s)",
                    status,
                    example.id,
                    result.turn_count,
                    result.cost_usd,
                    f" judge={result.judge_score:.2f}" if result.judge_score is not None else "",
                    f" reason={result.failure_reason!r}" if not result.passed else "",
                )
                return result

        tasks = [_run_one(ex) for ex in examples]
        results = await asyncio.gather(*tasks)

        report = EvalReport(
            dataset_name=dataset.name,
            dataset_version=dataset.version,
            tag=self._tag,
            created_at=time.time(),
            results=list(results),
            model=self._model,
        )
        logger.info(
            "EvalRunner done: pass_rate=%.1f%% mean_turns=%.1f total_cost=$%.4f",
            report.pass_rate * 100,
            report.mean_turns,
            report.total_cost_usd,
        )
        return report

    async def _apply_judge(
        self,
        example: GoldenExample,
        run: AgentRun,
        result: ExampleResult,
    ) -> ExampleResult:
        """Score a hard-gate survivor with the LLM judge."""
        assert self._judge is not None
        verdict = await self._judge.evaluate(
            run,
            reference_tools=example.expected_tool_sequence,
            constraints=example.constraints,
        )
        result.judge_score = verdict.overall_score
        result.metadata["judge"] = {
            "overall_score": verdict.overall_score,
            "trajectory_match": verdict.trajectory_match,
            "reasoning_summary": verdict.reasoning_summary,
            "dimensions": {
                d.name: {"score": d.score, "blocking": d.is_blocking, "reasoning": d.reasoning}
                for d in verdict.dimension_scores
            },
        }
        if verdict.judge_error:
            # Judge infra flake must not veto a run that passed the hard gate.
            result.metadata["judge_error"] = True
            result.metadata["judge_error_reason"] = verdict.reasoning_summary
            logger.warning(
                "EvalRunner: judge error on example %r — not vetoing (reason=%s)",
                example.id,
                verdict.reasoning_summary,
            )
            return result
        if not verdict.is_releasable():
            result.passed = False
            if verdict.blocking_failures:
                blockers = ", ".join(f"{d.name}={d.score:.2f}" for d in verdict.blocking_failures)
                result.failure_reason = f"Judge blocked release: {blockers}"
            else:
                result.failure_reason = (
                    f"Judge overall_score {verdict.overall_score:.2f} below threshold"
                )
        return result

    @staticmethod
    def save_report(report: EvalReport, evals_dir: str | Path) -> Path:
        """Persist *report* to {evals_dir}/_reports/{dataset_name}/{tag}.json."""
        dir_path = Path(evals_dir) / "_reports" / report.dataset_name
        dir_path.mkdir(parents=True, exist_ok=True)
        path = dir_path / f"{report.tag}.json"
        tmp = path.with_suffix(".tmp")
        tmp.write_text(
            json.dumps(report.to_dict(), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        tmp.replace(path)
        logger.info("EvalReport saved: %s", path)
        return path

    @staticmethod
    def load_report(evals_dir: str | Path, dataset_name: str, tag: str) -> EvalReport:
        path = Path(evals_dir) / "_reports" / dataset_name / f"{tag}.json"
        data = json.loads(path.read_text(encoding="utf-8"))
        return EvalReport.from_dict(data)


def _welch_t_pvalue(a: list[float], b: list[float]) -> float:
    """One-tailed Welch's t-test: P(mean(b) > mean(a)).

    Normal approximation when df >= 30. Returns 1.0 (no evidence) on degenerate inputs.
    """
    if len(a) < 2 or len(b) < 2:
        return 1.0
    mean_a = statistics.fmean(a)
    mean_b = statistics.fmean(b)
    var_a = statistics.variance(a, mean_a)
    var_b = statistics.variance(b, mean_b)
    se = math.sqrt(var_a / len(a) + var_b / len(b))
    if se == 0:
        return 1.0
    t = (mean_b - mean_a) / se  # positive = b is larger (regression)
    return _normal_cdf(-t)


def _normal_cdf(z: float) -> float:
    """Standard normal CDF P(Z <= z)."""
    return statistics.NormalDist().cdf(z)


def _wilson_interval(successes: int, n: int, z: float = 1.96) -> tuple[float, float]:
    """Wilson score confidence interval for a binomial proportion."""
    if n == 0:
        return (0.0, 0.0)
    p = successes / n
    denom = 1 + z * z / n
    centre = (p + z * z / (2 * n)) / denom
    spread = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / denom
    return (max(0.0, centre - spread), min(1.0, centre + spread))


def _two_proportion_z_test(pass_a: int, n_a: int, pass_b: int, n_b: int) -> tuple[float, str]:
    """One-tailed two-proportion test: P(pass_rate(b) < pass_rate(a))."""
    if n_a == 0 or n_b == 0:
        return 1.0, "degenerate (n=0)"
    p_a = pass_a / n_a
    p_b = pass_b / n_b
    if p_a == p_b:
        return 0.5, "equal rates"

    if n_a < 30 or n_b < 30:
        lo_a, hi_a = _wilson_interval(pass_a, n_a)
        lo_b, hi_b = _wilson_interval(pass_b, n_b)
        if hi_b < lo_a:
            return 0.0, f"wilson (n_a={n_a}, n_b={n_b}) — b interval below a"
        if lo_b > hi_a:
            return 1.0, f"wilson (n_a={n_a}, n_b={n_b}) — b interval above a"
        return 0.5, f"wilson (n_a={n_a}, n_b={n_b}) — intervals overlap"

    p_pool = (pass_a + pass_b) / (n_a + n_b)
    if p_pool == 0 or p_pool == 1:
        return 1.0, "z-test (degenerate pooled proportion)"
    se = math.sqrt(p_pool * (1 - p_pool) * (1 / n_a + 1 / n_b))
    if se == 0:
        return 1.0, "z-test (zero se)"
    z = (p_b - p_a) / se
    return _normal_cdf(z), "z-test"


class RegressionLevel(StrEnum):
    PASS = "pass"
    WARN = "warn"
    FAIL = "fail"


@dataclass
class MetricComparison:
    metric: str
    baseline: float
    current: float
    delta: float
    p_value: float
    level: RegressionLevel
    note: str = ""


@dataclass
class RegressionResult:
    """Output of RegressionDetector.compare()."""

    level: RegressionLevel
    comparisons: list[MetricComparison] = field(default_factory=list)
    baseline_tag: str = ""
    current_tag: str = ""
    dataset_version_match: bool = True
    notes: list[str] = field(default_factory=list)

    @property
    def blocking(self) -> bool:
        return self.level == RegressionLevel.FAIL

    def summary(self) -> str:
        lines = [
            f"Regression check: {self.baseline_tag!r} → {self.current_tag!r}  [{self.level.value.upper()}]"
        ]
        for c in self.comparisons:
            sign = "▲" if c.delta > 0 else "▼"
            lines.append(
                f"  {c.metric:20s}: {c.baseline:.4f} → {c.current:.4f} "
                f"({sign}{abs(c.delta):.4f}, p={c.p_value:.3f})  [{c.level.value}]"
            )
        for n in self.notes:
            lines.append(f"  NOTE: {n}")
        return "\n".join(lines)


class RegressionDetector:
    """Compares two EvalReports and classifies regressions."""

    def __init__(
        self,
        *,
        warn_p: float = 0.10,
        fail_p: float = 0.05,
    ) -> None:
        self._warn_p = warn_p
        self._fail_p = fail_p

    def compare(self, baseline: EvalReport, current: EvalReport) -> RegressionResult:
        """Compare *current* against *baseline*. Returns a RegressionResult.

        Metrics: pass_rate (two-proportion z-test, one-tailed: current < baseline;
        Wilson intervals for small n); cost_usd and turn_count (Welch t-test,
        one-tailed: current > baseline).
        """
        comparisons: list[MetricComparison] = []
        notes: list[str] = []

        version_match = baseline.dataset_version == current.dataset_version
        if not version_match:
            notes.append(
                f"Dataset version mismatch: baseline={baseline.dataset_version[:12]} "
                f"current={current.dataset_version[:12]} — some examples may differ"
            )

        n_b = len(baseline.results)
        n_c = len(current.results)
        pass_b = sum(1 for r in baseline.results if r.passed)
        pass_c = sum(1 for r in current.results if r.passed)
        p_pass, pass_note = _two_proportion_z_test(pass_b, n_b, pass_c, n_c)
        comparisons.append(
            self._classify(
                "pass_rate",
                pass_b / n_b if n_b else 0.0,
                pass_c / n_c if n_c else 0.0,
                p_pass,
                regression_is_increase=False,
                note=pass_note,
            )
        )

        costs_b = [r.cost_usd for r in baseline.results]
        costs_c = [r.cost_usd for r in current.results]
        p_cost = _welch_t_pvalue(costs_b, costs_c)
        comparisons.append(
            self._classify(
                "cost_usd",
                sum(costs_b) / len(costs_b) if costs_b else 0.0,
                sum(costs_c) / len(costs_c) if costs_c else 0.0,
                p_cost,
                regression_is_increase=True,
            )
        )

        turns_b = [float(r.turn_count) for r in baseline.results]
        turns_c = [float(r.turn_count) for r in current.results]
        p_turns = _welch_t_pvalue(turns_b, turns_c)
        comparisons.append(
            self._classify(
                "turn_count",
                sum(turns_b) / len(turns_b) if turns_b else 0.0,
                sum(turns_c) / len(turns_c) if turns_c else 0.0,
                p_turns,
                regression_is_increase=True,
            )
        )

        levels = [c.level for c in comparisons]
        if RegressionLevel.FAIL in levels:
            overall = RegressionLevel.FAIL
        elif RegressionLevel.WARN in levels:
            overall = RegressionLevel.WARN
        else:
            overall = RegressionLevel.PASS

        return RegressionResult(
            level=overall,
            comparisons=comparisons,
            baseline_tag=baseline.tag,
            current_tag=current.tag,
            dataset_version_match=version_match,
            notes=notes,
        )

    def _classify(
        self,
        metric: str,
        baseline_val: float,
        current_val: float,
        p_value: float,
        *,
        regression_is_increase: bool,
        note: str = "",
    ) -> MetricComparison:
        delta = current_val - baseline_val
        is_regression = delta > 0 if regression_is_increase else delta < 0

        if not is_regression or p_value > self._warn_p:
            level = RegressionLevel.PASS
        elif p_value > self._fail_p:
            level = RegressionLevel.WARN
        else:
            level = RegressionLevel.FAIL

        return MetricComparison(
            metric=metric,
            baseline=baseline_val,
            current=current_val,
            delta=delta,
            p_value=p_value,
            level=level,
            note=note,
        )
