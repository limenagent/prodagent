"""ir — the compile gate's front-end home.

The unified intermediate form is the Graph itself (kernel/graph.py); the
model and degenerate front-ends' entries (``compile_planned``) lives there now. This package keeps the hand-written
front-end (``compile_workflow``) and re-exports the validator for the
paths that cite it by its historical home.
"""

from prodagent.kernel.graph_validator import PlanIssue, PlanValidationError, PlanValidator
from prodagent.plan.ir.compiler import compile_workflow

__all__ = [
    "PlanIssue",
    "PlanValidationError",
    "PlanValidator",
    "compile_workflow",
]
