"""Compiler — the hand-written front-end's gate.

``compile_planned`` (the model
front-ends) now live in ``kernel.graph`` next to the Plan they build; this
module keeps what genuinely belongs above the kernel: compiling a
``Workflow`` — a plan *builder* that knows about Agents and fn tables.
Same gate (kernel's five-check validator), different timing — the trust
split of column 8, decided by ``Origin``.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from prodagent.kernel.graph import Origin, Plan
from prodagent.kernel.graph_validator import PlanValidator

if TYPE_CHECKING:
    import inspect
    from collections.abc import Mapping

    from prodagent.plan.workflow import Workflow

__all__ = ["compile_workflow"]


def _validator(fn_sigs: Mapping[str, inspect.Signature] | None = None) -> PlanValidator:
    return PlanValidator(fn_sigs=fn_sigs) if fn_sigs else PlanValidator()


def compile_workflow(
    wf: Workflow, *, fn_sigs: Mapping[str, inspect.Signature] | None = None
) -> Plan:
    """Workflow declarations → validate(origin=STATIC) → Plan.

    A hand-written cycle or a dangling edge fails HERE, at compile time,
    with the offender named — not as a hang at run."""
    nodes = wf.node_declarations()
    _validator(fn_sigs).validate_nodes(nodes)
    plan = Plan(origin=Origin.STATIC)
    plan.add_nodes(nodes)
    return plan
