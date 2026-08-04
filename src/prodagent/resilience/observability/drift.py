"""Drift detection — compare a run's span sequence to a golden one."""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field


@dataclass(frozen=True)
class Drift:
    kind: str  # "skipped" | "extra" | "substituted" | "reordered"
    detail: str


@dataclass
class DriftReport:
    """Result of comparing a run's action sequence to its golden trajectory."""

    drifted: bool
    drifts: list[Drift] = field(default_factory=list)

    @property
    def kinds(self) -> set[str]:
        return {d.kind for d in self.drifts}

    def __bool__(self) -> bool:
        return self.drifted


class DriftDetector:
    """Compare a run's action sequence to a golden trajectory."""

    def compare(self, golden: list[str], actual: list[str]) -> DriftReport:
        drifts: list[Drift] = []

        golden_counter = Counter(golden)
        actual_counter = Counter(actual)
        if golden_counter == actual_counter and golden != actual:
            drifts.append(
                Drift(
                    kind="reordered",
                    detail=f"same actions, different order: {actual!r} vs {golden!r}",
                )
            )
            return DriftReport(drifted=True, drifts=drifts)

        for action in set(golden) - set(actual):
            drifts.append(Drift(kind="skipped", detail=f"missing action: {action!r}"))

        for action in set(actual) - set(golden):
            drifts.append(Drift(kind="extra", detail=f"unexpected action: {action!r}"))

        if len(golden) == len(actual):
            for i, (g, a) in enumerate(zip(golden, actual, strict=True)):
                if g != a:
                    drifts.append(
                        Drift(
                            kind="substituted",
                            detail=f"position {i}: expected {g!r}, got {a!r}",
                        )
                    )

        return DriftReport(drifted=bool(drifts), drifts=drifts)
