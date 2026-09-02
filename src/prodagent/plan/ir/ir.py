"""PlanIR — the unified intermediate representation between sources and Plan.

Three front-ends (hand-written Workflow, model planner, the REACTIVE
degenerate case) all produce the same resolved-but-not-yet-instantiated
shape: a tuple of frozen nodes plus a lineage label. That shape is what a
PlanValidator can check repeatedly and what the compiler freezes into a
Plan — the compiler's multi-front-end / single-backend structure of
column 8, with ``Origin`` carrying the trust level through unchanged.

The IR reuses :class:`~prodagent.plan.dag.Node` deliberately: the node
already is resolved, frozen, serializable wire-shaped data. A second
NodeSpec would only drift from it.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from prodagent.plan.dag import Origin

if TYPE_CHECKING:
    from collections.abc import Iterable

    from prodagent.plan.dag import Node
    from prodagent.plan.ir.validator import PlanValidator

__all__ = ["PlanIR"]


@dataclass(frozen=True)
class PlanIR:
    """Resolved, uninstanciated, re-checkable — the standard part every
    graph source emits and the validator consumes."""

    nodes: tuple[Node, ...]
    origin: Origin = Origin.PLANNED
    revision: int = 1

    @classmethod
    def of(cls, nodes: Iterable[Node], *, origin: Origin, revision: int = 1) -> PlanIR:
        return cls(nodes=tuple(nodes), origin=origin, revision=revision)

    def validate(self, validator: PlanValidator) -> None:
        validator.validate(list(self.nodes))

    def is_empty(self) -> bool:
        return not self.nodes
