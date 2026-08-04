"""Eval runner — wires EvalRunner + LLMJudge + RegressionDetector.

  1. EvalRunner         — drives the GoldenDataset through the REAL agent
     (evals.factory), applies the hard gate (forbidden tools / tool subsequence
     / turn budget), and — when a judge is passed — scores every hard-gate
     survivor and lets a blocking safety-dimension failure veto the pass.
  2. LLMJudge            — the independent grader (evals.factory.build_judge).
  3. RegressionDetector  — in --ci mode, compares this run's report against
     the saved baseline and returns a non-zero exit code on a FAIL-level
     regression.

The default (``--smoke``, wired to ``make evals``) runs exactly one real case
through the full stack so a human can eyeball a live trajectory in ~1 call.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from pathlib import Path

from prodagent.evaluation.evals.runner import EvalRunner, RegressionDetector, RegressionLevel

from evals.dataset import GOLDEN
from evals.factory import build_judge, run_example

_EVALS_DIR = Path(__file__).resolve().parent
_BASELINE_TAG = "baseline"


def _print_report(report, *, header: str) -> None:
    print(f"\n{'═' * 64}")
    print(f"  {header}")
    print(f"{'═' * 64}")
    print(json.dumps(report.to_dict(), indent=2))
    for r in report.results:
        status = "PASS" if r.passed else "FAIL"
        line = f"  [{status}] {r.example_id}  turns={r.turn_count} cost=${r.cost_usd:.4f}"
        if r.judge_score is not None:
            line += f" judge={r.judge_score:.2f}"
        print(line)
        print(f"         tools: {' → '.join(r.tool_sequence) or '(none)'}")
        if not r.passed and r.failure_reason:
            print(f"         reason: {r.failure_reason}")
        judge = r.metadata.get("judge")
        if judge:
            dims = ", ".join(
                f"{name}={d['score']:.2f}{'*' if d['blocking'] else ''}"
                for name, d in judge["dimensions"].items()
            )
            print(f"         judge: {dims}  (traj_match={judge['trajectory_match']:.2f})")
            if judge.get("reasoning_summary"):
                print(f"         verdict: {judge['reasoning_summary'][:140]}")


async def _run(*, tag: str, use_judge: bool, tags: list[str] | None):
    model = (
        os.getenv("AGENT_FLAGSHIP_MODEL")
        or os.getenv("ANTHROPIC_MODEL")
        or os.getenv("AGENT_MODEL")
        or os.getenv("OLLAMA_MODEL")
        or "auto-detected"
    )
    print(f"[evals] agent LLM model: {model}")
    judge = None
    if use_judge:
        judge = build_judge()
        print("[evals] judge: enabled (safety_compliance is the blocking dimension)")

    runner = EvalRunner(run_example, tag=tag, model=model, judge=judge)
    report = await runner.run(GOLDEN, tags=tags)
    return runner, report


def _cmd_smoke() -> int:
    runner, report = asyncio.run(_run(tag="smoke", use_judge=True, tags=["smoke"]))
    _print_report(report, header="SMOKE — one real case through the full stack")
    runner.save_report(report, _EVALS_DIR)
    # Smoke is informational: a green light means the stack ran end-to-end.
    return 0 if report.results else 1


def _cmd_full(tag: str, *, save_baseline: bool) -> int:
    runner, report = asyncio.run(_run(tag=tag, use_judge=True, tags=None))
    _print_report(report, header=f"FULL RUN — tag={tag!r}")
    runner.save_report(report, _EVALS_DIR)
    if save_baseline:
        report.tag = _BASELINE_TAG
        path = runner.save_report(report, _EVALS_DIR)
        print(f"[evals] baseline saved → {path}")
    return 0 if report.pass_rate == 1.0 else 1


def _cmd_ci(tag: str) -> int:
    runner, current = asyncio.run(_run(tag=tag, use_judge=True, tags=None))
    _print_report(current, header=f"CI RUN — tag={tag!r}")
    runner.save_report(current, _EVALS_DIR)

    try:
        baseline = runner.load_report(_EVALS_DIR, GOLDEN.name, _BASELINE_TAG)
    except FileNotFoundError:
        print("[evals] no baseline yet — capturing this run as baseline.")
        current.tag = _BASELINE_TAG
        runner.save_report(current, _EVALS_DIR)
        return 0 if current.pass_rate == 1.0 else 1

    detector = RegressionDetector()
    result = detector.compare(baseline, current)
    print(f"\n{result.summary()}")

    if result.level == RegressionLevel.FAIL:
        print("[evals] BLOCKING regression — failing CI.")
        return 2
    if current.pass_rate < 1.0:
        print("[evals] hard-gate/judge failure in current run — failing CI.")
        return 1
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(prog="evals.runner", description="AIOps agent evals")
    parser.add_argument("--smoke", action="store_true", help="One real case through the full stack")
    parser.add_argument("--baseline", action="store_true", help="Full run, save as baseline")
    parser.add_argument("--ci", action="store_true", help="Full run + regression gate vs baseline")
    parser.add_argument("--tag", default="local", help="Report tag for a full run")
    args = parser.parse_args()

    if args.smoke:
        return _cmd_smoke()
    if args.ci:
        return _cmd_ci(args.tag)
    if args.baseline:
        return _cmd_full(args.tag, save_baseline=True)
    return _cmd_full(args.tag, save_baseline=False)


if __name__ == "__main__":
    sys.exit(main())
