"""The dependency graph is acyclic — a theorem, not a convention.

``test_layering_contract`` pins *pairwise* rules (which package may never
import which), but pairwise bans cannot see a transitive cycle
(a → b → c → a): each edge can be individually legal while the whole is
still impossible to load in isolation or to explain top-down.

This test builds the full module-level import graph of ``src/prodagent``
(runtime edges only — ``if TYPE_CHECKING`` blocks carry no dependency) and
runs Kahn's algorithm on it. A cycle fails the test naming every module on
one; acyclicity means the packages can always be presented in a bottom-up
teaching order where every import points at something already defined.
"""

from __future__ import annotations

import ast
from pathlib import Path

SRC = Path(__file__).resolve().parents[2] / "src" / "prodagent"


def _module_name(path: Path) -> str:
    rel = path.relative_to(SRC)
    parts = list(rel.with_suffix("").parts)
    if parts[-1] == "__init__":
        parts = parts[:-1]
    return "prodagent." + ".".join(parts) if parts else "prodagent"


def _is_type_checking_guard(node: ast.If) -> bool:
    test = node.test
    return (isinstance(test, ast.Name) and test.id == "TYPE_CHECKING") or (
        isinstance(test, ast.Attribute) and test.attr == "TYPE_CHECKING"
    )


def _runtime_import_targets(tree: ast.Module) -> set[str]:
    """Module-scope prodagent.* imports, excluding TYPE_CHECKING-guarded ones
    (same exemption philosophy as test_layering_contract)."""
    guarded: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.If) and _is_type_checking_guard(node):
            for sub in ast.walk(node):
                if isinstance(sub, (ast.Import, ast.ImportFrom)):
                    guarded.add(sub.lineno)

    targets: set[str] = set()
    for node in tree.body:
        if not isinstance(node, (ast.Import, ast.ImportFrom)):
            continue
        if node.lineno in guarded:
            continue
        names = (
            [a.name for a in node.names] if isinstance(node, ast.Import) else [node.module or ""]
        )
        for name in names:
            if name == "prodagent" or name.startswith("prodagent."):
                targets.add(name)
    return targets


def _build_graph() -> dict[str, set[str]]:
    graph: dict[str, set[str]] = {}
    for path in sorted(SRC.rglob("*.py")):
        if "__pycache__" in path.parts or path.name == "__main__.py":
            continue
        mod = _module_name(path)
        tree = ast.parse(path.read_text(encoding="utf-8"))
        graph[mod] = _runtime_import_targets(tree)
    return graph


def _resolve(target: str, modules: set[str]) -> str | None:
    """Longest prefix of *target* that is a module in the graph —
    ``prodagent.kernel.budget`` matches itself; ``prodagent.base`` matches
    the package even when only its ``__init__`` exists."""
    parts = target.split(".")
    for i in range(len(parts), 0, -1):
        prefix = ".".join(parts[:i])
        if prefix in modules:
            return prefix
    return None


def test_no_import_cycles() -> None:
    graph = _build_graph()
    modules = set(graph)

    edges: dict[str, set[str]] = {m: set() for m in modules}
    for mod, targets in graph.items():
        for target in targets:
            resolved = _resolve(target, modules)
            if resolved is not None and resolved != mod:
                edges[mod].add(resolved)

    # Kahn's algorithm — leftover nodes after peeling in-degree zeros are cycles.
    indegree = {m: 0 for m in modules}
    for mod, deps in edges.items():
        for _dep in deps:
            indegree[mod] += 1
    dependents: dict[str, set[str]] = {m: set() for m in modules}
    for mod, deps in edges.items():
        for dep in deps:
            dependents[dep].add(mod)

    ready = sorted(m for m, d in indegree.items() if d == 0)
    peeled: list[str] = []
    while ready:
        node = ready.pop()
        peeled.append(node)
        for dependent in dependents[node]:
            indegree[dependent] -= 1
            if indegree[dependent] == 0:
                ready.append(dependent)

    cyclic = sorted(set(modules) - set(peeled))
    assert not cyclic, (
        "Import cycle(s) in src/prodagent (runtime edges, TYPE_CHECKING "
        "exempt) — modules on a cycle:\n  " + "\n  ".join(cyclic)
    )
