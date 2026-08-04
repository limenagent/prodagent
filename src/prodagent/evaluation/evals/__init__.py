"""Eval suite — golden datasets, LLM-as-Judge, regression detection, statistical CI gates."""

from prodagent.evaluation.evals.dataset import (
    EvalReport,
    ExampleResult,
    GoldenDataset,
    GoldenExample,
)
from prodagent.evaluation.evals.judge import (
    DimensionScore,
    JudgeVerdict,
    LLMJudge,
    compare_trajectories,
)
from prodagent.evaluation.evals.runner import EvalRunner, RegressionDetector, RegressionLevel

__all__ = [
    # Dataset
    "GoldenDataset",
    "GoldenExample",
    "EvalReport",
    "ExampleResult",
    # Runner + regression
    "EvalRunner",
    "RegressionDetector",
    "RegressionLevel",
    # LLM-as-Judge
    "LLMJudge",
    "JudgeVerdict",
    "DimensionScore",
    "compare_trajectories",
]
