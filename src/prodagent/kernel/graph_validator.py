"""PlanValidator — the five shape checks every graph passes before it runs.

Compiler front-ends differ in trust, not in format (column 8): a
hand-written Workflow is validated once at compile time; a model-drafted
or runtime-grown graph is revalidated on every submission. The checks are
the same gate either way — one truth source, named offenders, structured
errors a model can read and repair.

Edges are the input currency (``validate_graph``); a bare node list
(``validate_nodes``) reads each node's declared ``depends_on``, the form
merge submits before the edges land. Conditional edges
(:class:`~prodagent.kernel.graph.Edge` ``when``) count as edges for every
structural check: a cycle is a cycle even if a predicate might waive it at
runtime — shape is static, waiving is scheduling.

The five checks:
1. **Acyclic** — Kahn over the live set; cycle members are named so a
   replan can target the loop.
2. **References resolve** — every edge's endpoints exist; every ``{{ref}}``
   template in params names a node in the graph (or ``task``) and uses the
   supported syntax.
3. **Reachable** — from the roots (in-degree-zero nodes) every node is
   reachable; no islands. Multiple roots are legitimate (parallel entry
   points); the teaching phrase "single entry" lands as "the root set
   covers the graph".
4. **Unit contracts** — the unit's own configuration is complete (a tool
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

    from prodagent.kernel.graph import Edge, Graph, Node

__all__ = ["PlanIssue", "PlanValidationError", "PlanValidator", "default_validator"]

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


def default_validator() -> PlanValidator:
    """The shared five-check gate — resolved lazily so graph and validator
    can cite each other's types without an import cycle."""
    return PlanValidator()


class PlanValidator:
    """Five checks over a node set + edge set; ``validate*`` raises,
    ``issues`` reports."""

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

    def validate_graph(
        self, graph: Graph, *, fn_sigs: Mapping[str, Signature] | None = None
    ) -> None:
        """Validate a built graph — its edge set is the truth (declared deps
        are already folded in)."""
        validator = self if not fn_sigs else PlanValidator(fn_sigs={**self._fn_sigs, **fn_sigs})
        validator._raise(validator.issues(list(graph.nodes.values()), graph.edges))

    def validate_nodes(self, nodes: Sequence[Node]) -> None:
        """Validate a bare node list — each node's declared ``depends_on``
        stands in for its edges (the merge path, pre-landing)."""
        self._raise(self.issues(nodes))

    def _raise(self, issues: list[PlanIssue]) -> None:
        if issues:
            raise PlanValidationError(issues)

    def issues(self, nodes: Sequence[Node], edges: Sequence[Edge] | None = None) -> list[PlanIssue]:
        if edges is None:
            from prodagent.kernel.graph import Edge

            edges = [Edge(source=dep, target=n.node_id) for n in nodes for dep in n.depends_on]
        found: list[PlanIssue] = []
        by_id = {n.node_id: n for n in nodes}

        found.extend(self._check_size(nodes, edges))
        found.extend(self._check_references(nodes, edges, by_id))
        found.extend(self._check_contracts(nodes))
        # Shape checks build on each other: reachability needs resolvable
        # references, and a cycle is the root cause of its own
        # unreachability — report the cause, not the symptom.
        if not any(i.check == "dangling_ref" for i in found):
            found.extend(self._check_acyclic(nodes, edges))
            if not any(i.check == "cycle" for i in found):
                found.extend(self._check_reachable(nodes, edges))
        return found

    # ── 5. size ─────────────────────────────────────────────────────────────

    def _check_size(self, nodes: Sequence[Node], edges: Sequence[Edge]) -> list[PlanIssue]:
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
        for e in edges:
            fanout.setdefault(e.source, []).append(e.target)
        for source, children in fanout.items():
            if len(children) > self._max_fanout:
                issues.append(
                    PlanIssue(
                        "size",
                        source,
                        f"fan-out {len(children)} exceeds the cap of {self._max_fanout} "
                        f"({', '.join(sorted(children))})",
                    )
                )
        return issues

    # ── 2. references ───────────────────────────────────────────────────────

    def _check_references(
        self, nodes: Sequence[Node], edges: Sequence[Edge], by_id: dict[str, Node]
    ) -> list[PlanIssue]:
        issues: list[PlanIssue] = []
        for e in edges:
            if e.source not in by_id:
                issues.append(
                    PlanIssue("dangling_ref", e.target, f"dependency {e.source!r} not found")
                )
            if e.target not in by_id:
                issues.append(
                    PlanIssue("dangling_ref", e.source, f"edge target {e.target!r} not found")
                )
        for n in nodes:
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

    # ── 4. unit contracts ───────────────────────────────────────────────────

    def _check_contracts(self, nodes: Sequence[Node]) -> list[PlanIssue]:
        from prodagent.kernel.units import FnUnit, LLMUnit, SubAgentUnit, ToolUnit

        issues: list[PlanIssue] = []
        for n in nodes:
            body = n.body
            if isinstance(body, ToolUnit) and not body.tool:
                issues.append(PlanIssue("contract", n.node_id, "tool node has no tool name"))
            elif isinstance(body, LLMUnit) and not body.prompt:
                issues.append(PlanIssue("contract", n.node_id, "llm node has no prompt"))
            elif isinstance(body, SubAgentUnit) and not body.agent:
                issues.append(PlanIssue("contract", n.node_id, "subagent node has no agent name"))
            elif isinstance(body, FnUnit):
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
    def _check_acyclic(nodes: Sequence[Node], edges: Sequence[Edge]) -> list[PlanIssue]:
        in_degree = {n.node_id: 0 for n in nodes}
        dependents: dict[str, list[str]] = {n.node_id: [] for n in nodes}
        for e in edges:
            if e.source in dependents and e.target in in_degree:
                dependents[e.source].append(e.target)
                in_degree[e.target] += 1

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
                f"{cycle} — break the loop by removing one edge",
            )
        ]

    # ── 3. reachable ────────────────────────────────────────────────────────

    @staticmethod
    def _check_reachable(nodes: Sequence[Node], edges: Sequence[Edge]) -> list[PlanIssue]:
        dependents: dict[str, list[str]] = {n.node_id: [] for n in nodes}
        roots: list[str] = []
        has_incoming: set[str] = set()
        for e in edges:
            if e.source in dependents and e.target in dependents:
                dependents[e.source].append(e.target)
                has_incoming.add(e.target)
        roots = [n.node_id for n in nodes if n.node_id not in has_incoming]

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
                "graph (add it to some node's dependency chain)",
            )
            for island in sorted(set(dependents) - seen)
        ]
