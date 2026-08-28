"""Plan — the runtime, self-revising DAG that PLAN_FIRST execution produces."""

from __future__ import annotations

import re
import uuid
from collections import deque
from dataclasses import asdict, dataclass, field
from typing import Any

from prodagent.kernel.types import StepStatus


@dataclass
class PlanStep:
    step_id: str
    action: str = ""
    params: dict[str, Any] = field(default_factory=dict)
    depends_on: list[str] = field(default_factory=list)
    is_terminal: bool = False

    status: StepStatus = StepStatus.PENDING
    output_ref: Any = None
    error: str | None = None
    attempts: int = 0
    completed_at: float = 0.0

    version_created: int = 1
    replaces_step_id: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    def to_hook_dict(self, *, include_terminal: bool = False) -> dict[str, Any]:
        d: dict[str, Any] = {
            "id": self.step_id,
            "action": self.action,
            "params": self.params,
            "depends_on": self.depends_on,
        }
        if include_terminal:
            d["terminal"] = self.is_terminal
        return d


class Plan:
    """Versioned, self-revising DAG of PlanSteps."""

    def __init__(self, plan_id: str | None = None) -> None:
        self.plan_id = plan_id or str(uuid.uuid4())
        self.version: int = 1
        self._steps: dict[str, PlanStep] = {}
        self._dependents: dict[str, list[str]] = {}
        self.task_input: str = ""

    @classmethod
    def from_state(cls, state: dict[str, Any], *, plan_id: str) -> Plan:
        plan = cls(plan_id=plan_id)
        plan.version = state.get("version", 1)
        for sid, sd in state.get("steps", {}).items():
            step = PlanStep(
                step_id=sid,
                action=sd.get("action", ""),
                params=sd.get("params", {}),
                depends_on=sd.get("depends_on", []),
                is_terminal=sd.get("is_terminal", False),
                status=StepStatus(sd.get("status", "pending")),
                output_ref=sd.get("output_ref"),
                error=sd.get("error"),
                attempts=sd.get("attempts", 0),
                completed_at=sd.get("completed_at", 0.0),
                version_created=sd.get("version_created", plan.version),
                replaces_step_id=sd.get("replaces_step_id"),
            )
            if step.status is StepStatus.RUNNING:
                # Crashed mid-flight: output_ref/error are stale partial state.
                step.status = StepStatus.PENDING
                step.output_ref = None
                step.error = None
            plan._steps[sid] = step
            plan._index_deps(step)
        return plan

    def add_steps(self, steps: list[PlanStep]) -> None:
        self._assert_acyclic(steps)
        for s in steps:
            s.version_created = self.version
            self._steps[s.step_id] = s
            self._index_deps(s)

    def merge(self, new_steps: list[PlanStep]) -> None:
        # Replans rewrite by version, not in place: replaced steps turn
        # OBSOLETE and successors carry lineage (replaces_step_id), so the
        # event log replays every revision — nothing is silently mutated.
        new_ids = {s.step_id for s in new_steps}
        for ns in new_steps:
            for dep_id in ns.depends_on:
                dep = self._steps.get(dep_id)
                if dep is None:
                    if dep_id not in new_ids:
                        raise ValueError(f"Step {ns.step_id!r}: dependency {dep_id!r} not found")
                elif dep.status is StepStatus.OBSOLETE and dep_id not in new_ids:
                    raise ValueError(
                        f"Step {ns.step_id!r}: dependency {dep_id!r} is OBSOLETE "
                        f"(and not replaced in this merge)"
                    )

        self._assert_acyclic(new_steps)

        self.version += 1
        for ns in new_steps:
            if ns.replaces_step_id:
                old = self._steps.get(ns.replaces_step_id)
                if old:
                    old.status = StepStatus.OBSOLETE
            ns.version_created = self.version
            self._steps[ns.step_id] = ns
            self._index_deps(ns)

    def _index_deps(self, step: PlanStep) -> None:
        for dep_id in step.depends_on:
            self._dependents.setdefault(dep_id, []).append(step.step_id)

    def get_step(self, step_id: str) -> PlanStep | None:
        return self._steps.get(step_id)

    @property
    def steps(self) -> list[PlanStep]:
        return list(self._steps.values())

    def get_parallel_ready(self) -> list[PlanStep]:
        return [
            s for s in self._steps.values() if s.status is StepStatus.PENDING and self._deps_done(s)
        ]

    def requeue_suspended(self) -> None:
        """Flip SUSPENDED steps back to PENDING so they re-execute on resume."""
        for s in self._steps.values():
            if s.status is StepStatus.SUSPENDED:
                s.status = StepStatus.PENDING
                s.output_ref = None
                s.error = None

    def _deps_done(self, step: PlanStep) -> bool:
        for dep_id in step.depends_on:
            dep = self._steps.get(dep_id)
            if dep is None:
                raise ValueError(
                    f"Step {step.step_id!r} references unknown dependency {dep_id!r}. "
                    "Plan state is corrupted."
                )
            if dep.status is not StepStatus.COMPLETED:
                return False
        return True

    def is_complete(self) -> bool:
        return all(
            s.status in (StepStatus.COMPLETED, StepStatus.OBSOLETE) for s in self._steps.values()
        )

    def mark_downstream_obsolete(self, failed_step_id: str) -> list[str]:
        """COMPLETED dependents are skipped but traversed so their own PENDING downstream still gets obsoleted."""
        obsolete: list[str] = []
        queue: deque[str] = deque(self._dependents.get(failed_step_id, ()))
        seen: set[str] = set(queue)

        while queue:
            sid = queue.popleft()
            step = self._steps.get(sid)
            if step is None:
                continue
            if step.status is not StepStatus.COMPLETED:
                step.status = StepStatus.OBSOLETE
                obsolete.append(sid)
            for child_id in self._dependents.get(sid, ()):
                if child_id not in seen:
                    seen.add(child_id)
                    queue.append(child_id)

        return obsolete

    def to_state(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "steps": {s.step_id: s.to_dict() for s in self.steps},
        }

    def resolve_params(self, step: PlanStep) -> dict[str, Any]:
        result = _resolve(step.params, step, self)
        return result if isinstance(result, dict) else {}

    def _assert_acyclic(self, candidates: list[PlanStep]) -> None:
        # The planner is an LLM — a cycle here is a model mistake, not a
        # programmer's. Reject it at the seam with the offenders named, not
        # mid-execution as a mysterious hang.
        live: dict[str, PlanStep] = {
            sid: s for sid, s in self._steps.items() if s.status is not StepStatus.OBSOLETE
        }
        for s in candidates:
            live[s.step_id] = s

        in_degree: dict[str, int] = {sid: 0 for sid in live}
        dependents: dict[str, list[str]] = {sid: [] for sid in live}
        for step in live.values():
            for dep_id in step.depends_on:
                if dep_id not in live:
                    continue
                dependents[dep_id].append(step.step_id)
                in_degree[step.step_id] += 1

        ready = deque(sid for sid, deg in in_degree.items() if deg == 0)
        processed = 0
        while ready:
            node = ready.popleft()
            processed += 1
            for child in dependents[node]:
                in_degree[child] -= 1
                if in_degree[child] == 0:
                    ready.append(child)

        if processed != len(live):
            cycle = sorted(sid for sid, deg in in_degree.items() if deg > 0)
            raise ValueError(f"Cycle detected in plan DAG. Participating nodes: {cycle}")


_TEMPLATE_RE = re.compile(r"\{\{\s*([\w.]+)\s*\}\}")
_LOOSE_TEMPLATE_RE = re.compile(r"\{\{[^}]*\}\}")


def _resolve(value: Any, step: PlanStep, plan: Plan) -> Any:
    if isinstance(value, dict):
        return {k: _resolve(v, step, plan) for k, v in value.items()}
    if isinstance(value, list):
        return [_resolve(v, step, plan) for v in value]
    if not isinstance(value, str):
        return value

    matches = list(_TEMPLATE_RE.finditer(value))
    if not matches:
        if _LOOSE_TEMPLATE_RE.search(value):
            invalid = _LOOSE_TEMPLATE_RE.findall(value)
            raise ValueError(
                f"param chain: step {step.step_id!r} has unsupported template syntax {invalid[0]!r}. "
                f"Only '{{{{step_id}}}}', '{{{{step_id.output}}}}', or "
                f"'{{{{step_id.output.field}}}}' (single top-level key, no array index, "
                f"no nested path) are supported. Hardcode the concrete value instead."
            )
        return value
    if len(matches) == 1 and matches[0].span() == (0, len(value)):
        return _lookup(matches[0].group(1), step, plan)
    return _TEMPLATE_RE.sub(lambda m: str(_lookup(m.group(1), step, plan)), value)


def _lookup(ref: str, step: PlanStep, plan: Plan) -> Any:
    parts = ref.split(".")
    dep_id = parts[0]
    rest = parts[1:]

    if dep_id == "task":
        if rest:
            raise ValueError(
                f"param chain: step {step.step_id!r} — {{task}} has no sub-fields, got {ref!r}"
            )
        return plan.task_input

    if rest and rest[0] == "output":
        rest = rest[1:]
    dep = plan.get_step(dep_id)
    if dep is None:
        raise ValueError(
            f"param chain: step {step.step_id!r} references {dep_id!r} which is not in the plan"
        )
    if dep.status is not StepStatus.COMPLETED:
        raise ValueError(
            f"param chain: step {step.step_id!r} references {dep_id!r} but it is "
            f"{dep.status.value!r}, not COMPLETED"
        )
    if not rest:
        return dep.output_ref
    if len(rest) != 1:
        raise ValueError(
            f"param chain: step {step.step_id!r} — only single-key refs "
            f"({{step_id.field}} or {{step_id.output.field}} or {{step_id.output}}) "
            f"are supported, got {ref!r}"
        )
    key = rest[0]
    if not isinstance(dep.output_ref, dict) or key not in dep.output_ref:
        raise ValueError(f"param chain: step {dep_id!r}.output has no key {key!r}")
    return dep.output_ref[key]
