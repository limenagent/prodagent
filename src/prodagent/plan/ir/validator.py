"""PlanValidator — the five shape checks every graph passes before it runs.

Compiler front-ends differ in trust, not in format (column 8): a
hand-written Workflow is validated once at compile time; a model-drafted
or runtime-grown graph is revalidated on every submission. The checks are
the same gate either way — one truth source, named offenders, structured
errors a model can read and repair.

The five checks:
1. **Acyclic** — Kahn over the live set; cycle members are named so a
   replan can target the loop.
2. **References resolve** — every ``depends_on`` target exists; every
   ``{{ref}}`` template in params names a node in the graph (or ``task``)
   and uses the supported syntax.
3. **Reachable** — from the roots (in-degree-zero nodes) every node is
   reachable; no islands. Multiple roots are legitimate (parallel entry
   points); the teaching phrase "single entry" lands as "the root set
   covers the graph".
4. **Body contracts** — the body's own configuration is complete (a tool
   name, a prompt, an agent), and fn params fit the declared signature
   when one is available.
5. **Size budget** — node count, fan-out and depth caps; a runaway model
   that drafts a ten-thousand-node graph fails here, cheaply, before a
   single tool fires.
"""

from __future__ import annotations

import inspect
import re
from collections import deque
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence
    from inspect import Signature

    from prodagent.plan.dag import Node

__all__ = ["PlanIssue", "PlanValidationError", "PlanValidator"]

_MAX_NODES = 64
_MAX_FANOUT = 16
_MAX_DEPTH = 32

_TEMPLATE_RE = re.compile(r"\{\{\s*([\w.]+)\s*\}\}")
_LOOSE_TEMPLATE_RE = re.compile(r"\{\{[^}]*\}\}")


@dataclass(frozen=True)
class PlanIssue:
    """One failed check, with the offender named — the shape feedback a
    human (or a model) can act on."""

    check: str
    node: str
    detail: str

    def render(self) -> str:
        return f"[{self.check}] {self.node}: {self.detail}"


class PlanValidationError(ValueError):
    """A graph that may not run — carries every issue found, not just the
    first, so one repair round can address them all."""

    def __init__(self, issues: list[PlanIssue]) -> None:
        self.issues = issues
        super().__init__("invalid plan:\n  " + "\n  ".join(i.render() for i in issues))


class PlanValidator:
    """Five checks over a node set; ``validate`` raises, ``issues`` reports."""

    def __init__(
        self,
        *,
        max_nodes: int = _MAX_NODES,
        max_fanout: int = _MAX_FANOUT,
        max_depth: int = _MAX_DEPTH,
        fn_sigs: Mapping[str, Signature] | None = None,
    ) -> None:
        self._max_nodes = max_nodes
        self._max_fanout = max_fanout
        self._max_depth = max_depth
        self._fn_sigs = fn_sigs or {}

    def validate(self, nodes: Sequence[Node]) -> None:
        issues = self.issues(nodes)
        if issues:
            raise PlanValidationError(issues)

    def issues(self, nodes: Sequence[Node]) -> list[PlanIssue]:
        found: list[PlanIssue] = []
        by_id = {n.node_id: n for n in nodes}

        found.extend(self._check_size(nodes))
        found.extend(self._check_references(nodes, by_id))
        found.extend(self._check_contracts(nodes))
        # Shape checks build on each other: reachability needs resolvable
        # references, and a cycle is the root cause of its own
        # unreachability — report the cause, not the symptom.
        if not any(i.check == "dangling_ref" for i in found):
            found.extend(self._check_acyclic(nodes))
            if not any(i.check == "cycle" for i in found):
                found.extend(self._check_reachable(nodes))
        return found

    # ── 5. size ─────────────────────────────────────────────────────────────

    def _check_size(self, nodes: Sequence[Node]) -> list[PlanIssue]:
        issues: list[PlanIssue] = []
        if len(nodes) > self._max_nodes:
            issues.append(
                PlanIssue(
                    "size",
                    f"<graph:{len(nodes)}>",
                    f"{len(nodes)} nodes exceeds the cap of {self._max_nodes}",
                )
            )
        fanout: dict[str, list[str]] = {}
        for n in nodes:
            for dep in n.depends_on:
                fanout.setdefault(dep, []).append(n.node_id)
        for dep, children in fanout.items():
            if len(children) > self._max_fanout:
                issues.append(
                    PlanIssue(
                        "size",
                        dep,
                        f"fan-out {len(children)} exceeds the cap of {self._max_fanout} "
                        f"({', '.join(sorted(children))})",
                    )
                )
        return issues

    # ── 2. references ───────────────────────────────────────────────────────

    def _check_references(self, nodes: Sequence[Node], by_id: dict[str, Node]) -> list[PlanIssue]:
        issues: list[PlanIssue] = []
        for n in nodes:
            for dep in n.depends_on:
                if dep not in by_id:
                    issues.append(
                        PlanIssue("dangling_ref", n.node_id, f"dependency {dep!r} not found")
                    )
            issues.extend(self._template_issues(n, by_id))
        return issues

    @staticmethod
    def _template_issues(n: Node, by_id: dict[str, Node]) -> list[PlanIssue]:
        def walk(value: object) -> list[PlanIssue]:
            if isinstance(value, dict):
                return [i for v in value.values() for i in walk(v)]
            if isinstance(value, list):
                return [i for v in value for i in walk(v)]
            if not isinstance(value, str):
                return []
            loose = _LOOSE_TEMPLATE_RE.search(value)
            if loose and not _TEMPLATE_RE.search(value):
                return [
                    PlanIssue(
                        "dangling_ref",
                        n.node_id,
                        f"unsupported template syntax {loose.group(0)!r} in param {value[:60]!r}",
                    )
                ]
            return [
                PlanIssue(
                    "dangling_ref",
                    n.node_id,
                    f"template {m.group(0)!r} references {m.group(1).split('.')[0]!r} "
                    "which is not in the plan",
                )
                for m in _TEMPLATE_RE.finditer(value)
                if m.group(1).split(".")[0] not in by_id
                and m.group(1) != "task"
                and not m.group(1).startswith("shared.")
            ]

        return walk(dict(n.params))

    # ── 4. body contracts ───────────────────────────────────────────────────

    def _check_contracts(self, nodes: Sequence[Node]) -> list[PlanIssue]:
        from prodagent.kernel.bodies.base import FnBody, LLMBody, SubAgentBody, ToolBody

        issues: list[PlanIssue] = []
        for n in nodes:
            body = n.body
            if isinstance(body, ToolBody) and not body.tool:
                issues.append(PlanIssue("contract", n.node_id, "tool node has no tool name"))
            elif isinstance(body, LLMBody) and not body.prompt:
                issues.append(PlanIssue("contract", n.node_id, "llm node has no prompt"))
            elif isinstance(body, SubAgentBody) and not body.agent:
                issues.append(PlanIssue("contract", n.node_id, "subagent node has no agent name"))
            elif isinstance(body, FnBody):
                if not body.fn:
                    issues.append(PlanIssue("contract", n.node_id, "fn node has no function name"))
                elif sig := self._fn_sigs.get(body.fn):
                    declared = {
                        p
                        for p, par in sig.parameters.items()
                        if par.kind
                        in (inspect.Parameter.POSITIONAL_OR_KEYWORD, inspect.Parameter.KEYWORD_ONLY)
                    }
                    unknown = set(dict(n.params)) - declared
                    if unknown:
                        issues.append(
                            PlanIssue(
                                "contract",
                                n.node_id,
                                f"params {sorted(unknown)} are not accepted by fn "
                                f"{body.fn!r} (declares {sorted(declared)})",
                            )
                        )
        return issues

    # ── 1. acyclic ──────────────────────────────────────────────────────────

    @staticmethod
    def _check_acyclic(nodes: Sequence[Node]) -> list[PlanIssue]:
        in_degree = {n.node_id: 0 for n in nodes}
        dependents: dict[str, list[str]] = {n.node_id: [] for n in nodes}
        for n in nodes:
            for dep in n.depends_on:
                dependents[dep].append(n.node_id)
                in_degree[n.node_id] += 1

        ready = deque(nid for nid, deg in in_degree.items() if deg == 0)
        processed = 0
        while ready:
            nid = ready.popleft()
            processed += 1
            for child in dependents[nid]:
                in_degree[child] -= 1
                if in_degree[child] == 0:
                    ready.append(child)

        if processed == len(in_degree):
            return []
        # Nodes that never reached in-degree 0 are exactly the cycle's members.
        cycle = sorted(nid for nid, deg in in_degree.items() if deg > 0)
        return [
            PlanIssue(
                "cycle",
                cycle[0] if len(cycle) == 1 else "<cycle>",
                "Cycle detected in plan DAG. Participating nodes: "
                f"{cycle} — break the loop by removing one depends_on edge",
            )
        ]

    # ── 3. reachable ────────────────────────────────────────────────────────

    @staticmethod
    def _check_reachable(nodes: Sequence[Node]) -> list[PlanIssue]:
        dependents: dict[str, list[str]] = {n.node_id: [] for n in nodes}
        roots: list[str] = []
        for n in nodes:
            if not n.depends_on:
                roots.append(n.node_id)
            for dep in n.depends_on:
                dependents[dep].append(n.node_id)

        seen = set(roots)
        queue = deque(roots)
        while queue:
            nid = queue.popleft()
            for child in dependents[nid]:
                if child not in seen:
                    seen.add(child)
                    queue.append(child)

        return [
            PlanIssue(
                "unreachable",
                island,
                "unreachable from any root — every node must connect to the "
                "graph (add it to some node's depends_on chain)",
            )
            for island in sorted(set(dependents) - seen)
        ]
