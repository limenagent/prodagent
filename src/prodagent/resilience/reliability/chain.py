"""Chain reliability analysis."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from prodagent.runtime.plan.dag import Plan

_CRITICAL_THRESHOLD = 0.5
_WARNING_THRESHOLD = 0.75


class ReliabilityTier(StrEnum):
    CRITICAL = "CRITICAL"
    WARNING = "WARNING"
    OK = "OK"

    @property
    def description(self) -> str:
        return _TIER_DESCRIPTIONS[self]


_TIER_DESCRIPTIONS: dict[ReliabilityTier, str] = {
    ReliabilityTier.CRITICAL: "split this chain",
    ReliabilityTier.WARNING: "consider splitting",
    ReliabilityTier.OK: "OK",
}


@dataclass
class ChainReport:
    """Reliability diagnosis for a serial chain of n steps."""

    n_steps: int
    step_reliability: float
    serial_end_to_end: float
    severity: ReliabilityTier


@dataclass
class PlanChainReport:
    """Reliability diagnosis enriched with the critical path of a Plan DAG."""

    base: ChainReport
    total_steps: int = 0
    critical_path: list[str] = field(default_factory=list)
    critical_path_length: int = 0
    parallel_degree: float = 0.0


def _serial_reliability(step_reliability: float, n_steps: int) -> float:
    """R_serial = R^n — the chain killer."""
    return step_reliability**n_steps


class ChainOptimizer:
    """Reliability diagnostic for DAG-based execution plans."""

    def analyse(
        self,
        n_steps: int,
        step_reliability: float = 0.95,
    ) -> ChainReport:
        serial = _serial_reliability(step_reliability, n_steps)
        if serial < _CRITICAL_THRESHOLD:
            severity = ReliabilityTier.CRITICAL
        elif serial < _WARNING_THRESHOLD:
            severity = ReliabilityTier.WARNING
        else:
            severity = ReliabilityTier.OK
        return ChainReport(
            n_steps=n_steps,
            step_reliability=step_reliability,
            serial_end_to_end=round(serial, 4),
            severity=severity,
        )

    def analyse_plan(
        self,
        plan: Plan,
        step_reliability: float = 0.95,
    ) -> PlanChainReport:
        from prodagent.core.types import StepStatus

        steps = {s.step_id: s for s in plan.steps if s.status != StepStatus.OBSOLETE}

        if not steps:
            return PlanChainReport(base=self.analyse(0, step_reliability), total_steps=0)

        # Longest-path via memoised recursion (DAG — no cycles by invariant).
        depth: dict[str, int] = {}

        def _depth(step_id: str) -> int:
            if step_id in depth:
                return depth[step_id]
            step = steps.get(step_id)
            if step is None or not step.depends_on:
                depth[step_id] = 1
                return 1
            d = 1 + max(_depth(dep) for dep in step.depends_on if dep in steps)
            depth[step_id] = d
            return d

        for sid in steps:
            _depth(sid)

        critical_length = max(depth.values())

        sink = max(depth, key=lambda s: depth[s])
        path: list[str] = []
        current = sink
        while current:
            path.append(current)
            step = steps[current]
            deep_dep = max(
                (dep for dep in step.depends_on if dep in steps),
                key=lambda d: depth.get(d, 0),
                default=None,
            )
            current = deep_dep  # type: ignore[assignment]
        path.reverse()

        return PlanChainReport(
            base=self.analyse(critical_length, step_reliability),
            total_steps=len(steps),
            critical_path=path,
            critical_path_length=critical_length,
            parallel_degree=round(len(steps) / max(critical_length, 1), 1),
        )
