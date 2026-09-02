"""Compiler — every graph source freezes into a Plan through here.

``Workflow --compile--> PlanIR --> validate --> Plan``: the hand-written
path validates once, at compile time, and fails loudly (a human wrote the
bug — it should surface in their editor, not mid-run). The model path
(``compile_planned``) validates every submission, because every model
draft is a fresh claim. Same gate, different timing — the trust split of
column 8, decided by ``Origin``.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from prodagent.plan.dag import Node, Origin, Plan, react_plan
from prodagent.plan.ir.ir import PlanIR
from prodagent.plan.ir.validator import PlanValidator

if TYPE_CHECKING:
    import inspect
    from collections.abc import Mapping

    from prodagent.plan.workflow import Workflow

__all__ = ["compile_workflow", "compile_planned", "compile_reactive"]


def _validator(fn_sigs: Mapping[str, inspect.Signature] | None = None) -> PlanValidator:
    return PlanValidator(fn_sigs=fn_sigs) if fn_sigs else PlanValidator()


def compile_workflow(
    wf: Workflow, *, fn_sigs: Mapping[str, inspect.Signature] | None = None
) -> Plan:
    """WorkflowTemplate → PlanIR(origin=STATIC) → validate → Plan.

    A hand-written cycle or a dangling edge fails HERE, at compile time,
    with the offender named — not as a hang at run."""
    nodes = wf.node_declarations()
    PlanIR.of(nodes, origin=Origin.STATIC).validate(_validator(fn_sigs))
    plan = Plan(origin=Origin.STATIC)
    plan.add_nodes(nodes)
    return plan


def compile_planned(nodes: list[Node], *, revision: int = 1) -> Plan:
    """Planner output → PlanIR(origin=PLANNED) → validate → Plan.

    Every model draft revalidates: the model is an untrusted front-end
    that hallucinated edges and cycles will keep doing so."""
    PlanIR.of(nodes, origin=Origin.PLANNED, revision=revision).validate(PlanValidator())
    plan = Plan(origin=Origin.PLANNED)
    plan.add_nodes(list(nodes))
    return plan


def compile_reactive(plan_id: str | None = None) -> Plan:
    """The degenerate front-end: one node, no edges — trivially valid, but
    it passes the same gate so "every graph was validated" stays true."""
    plan = react_plan(plan_id)
    PlanValidator().validate(list(plan.nodes.values()))
    return plan
