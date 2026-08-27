"""Layering contract — pin the dependency direction in CI.

AST-scans imports across ``src/prodagent``:

- **module-level** imports follow the package rules below — in particular,
  ``prodagent.backends`` never appears at module level outside ``backends``;
- ``if TYPE_CHECKING`` blocks are allowed — type coupling carries no
  runtime dependency;
- function-body imports are allowed for lazy resolution — including
  ``prodagent.backends.factory``, the single resolution surface for default
  stores. The rule that matters is module-level: nothing outside
  ``backends`` may import ``prodagent.backends`` at module level, so the
  kernel import chain can never drag a backend in.

Rules (new packages/files are governed automatically):

- ``base`` depends on base only — the bottom layer (kernel imports base,
  never the reverse — shared vocabulary lives in ``base/types.py`` and is
  re-exported by ``kernel/types.py``);
- ``runtime`` ⇏ ``coordination`` — no runtime import of coordination may
  EXECUTE outside the assembly root: the peer relay and the settler arrive
  through the compose seam (``runtime/compose.py`` is the single exempt
  file; ``if TYPE_CHECKING`` type vocabulary, e.g. AgentConfig's
  ``MessageContract``/``Plan`` fields, carries no runtime dependency);
- ``ports`` depends on ports / base only;
- ``backends`` depends on backends / ports / base — storage must not
  reach into the business layer;
- ``playground`` / ``repl`` are top-level apps: nothing outside them may
  import them (``prodagent.__main__`` entry point is exempt);
- every other package (runtime / tooling / hooks / guardrail / llm /
  resilience / cognition / evaluation / mcp) must not import
  backends / playground / repl;
Any violation fails the test with a file:line list. Changing the layering
means changing this test too — same discipline as
``test_port_async_contract``.
"""

from __future__ import annotations

import ast
from pathlib import Path

SRC = Path(__file__).resolve().parents[2] / "src" / "prodagent"

# source package -> forbidden target package prefixes
FORBIDDEN: dict[str, frozenset[str]] = {
    "base": frozenset(
        {
            "runtime",
            "tooling",
            "hooks",
            "guardrail",
            "llm",
            "resilience",
            "cognition",
            "evaluation",
            "mcp",
            "backends",
            "playground",
            "repl",
            "bootstrap",
            "kernel",
            "ports",
            "plan",
            "coordination",
            "skills",
        }
    ),
    "ports": frozenset(
        {
            "runtime",
            "tooling",
            "hooks",
            "guardrail",
            "llm",
            "resilience",
            "cognition",
            "evaluation",
            "mcp",
            "backends",
            "playground",
            "repl",
            "bootstrap",
        }
    ),
    "backends": frozenset(
        {
            "runtime",
            "tooling",
            "hooks",
            "guardrail",
            "llm",
            "resilience",
            "cognition",
            "evaluation",
            "mcp",
            "playground",
            "repl",
        }
    ),
    "runtime": frozenset({"backends", "playground", "repl", "coordination"}),
    "tooling": frozenset({"backends", "playground", "repl"}),
    "hooks": frozenset({"backends", "playground", "repl"}),
    "guardrail": frozenset({"backends", "playground", "repl"}),
    "llm": frozenset({"backends", "playground", "repl"}),
    "resilience": frozenset({"backends", "playground", "repl"}),
    "cognition": frozenset({"backends", "playground", "repl"}),
    "evaluation": frozenset({"backends", "playground", "repl"}),
    "mcp": frozenset({"backends", "playground", "repl"}),
    "playground": frozenset({"repl"}),
    "repl": frozenset({"playground"}),
    "recipes": frozenset({"backends", "playground", "repl"}),
}

# entry-point exemption
EXEMPT_MODULES = frozenset({"__main__.py"})

TOP_LEVEL_PACKAGES = frozenset(FORBIDDEN)


def _source_pkg(path: Path) -> str | None:
    rel = path.relative_to(SRC)
    parts = rel.parts
    if len(parts) == 1:  # top-level single-file modules (__init__/__main__)
        return None
    return parts[0]


def _top_level_imports(tree: ast.Module) -> list[tuple[int, str]]:
    """Collect module-scope prodagent.* imports (nested scopes don't count)."""
    found: list[tuple[int, str]] = []
    for node in tree.body:
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name == "prodagent" or alias.name.startswith("prodagent."):
                    found.append((node.lineno, alias.name))
        elif isinstance(node, ast.ImportFrom):
            mod = node.module or ""
            if mod == "prodagent" or mod.startswith("prodagent."):
                found.append((node.lineno, mod))
        # If / Try / function / class bodies hold lazy or guarded imports: allowed
    return found


def _all_imports(tree: ast.Module) -> list[tuple[int, str]]:
    """Collect prodagent.* imports at ANY scope (module + nested bodies)."""
    found: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name == "prodagent" or alias.name.startswith("prodagent."):
                    found.append((node.lineno, alias.name))
        elif isinstance(node, ast.ImportFrom):
            mod = node.module or ""
            if mod == "prodagent" or mod.startswith("prodagent."):
                found.append((node.lineno, mod))
    return found


def _target_roots(imported: str) -> set[str]:
    """`prodagent.X.Y` -> {X}; bare `prodagent` -> {} (lazy root form)."""
    parts = imported.split(".")
    if parts[0] != "prodagent":
        return set()
    if len(parts) == 1:
        # `import prodagent` / `from prodagent import X`: the target lives in
        # names, which the lazy top-level __getattr__ resolves — allow it.
        return set()
    root = parts[1]
    return {root} if root in TOP_LEVEL_PACKAGES else set()


def _is_type_checking_guard(node: ast.If) -> bool:
    test = node.test
    return (isinstance(test, ast.Name) and test.id == "TYPE_CHECKING") or (
        isinstance(test, ast.Attribute) and test.attr == "TYPE_CHECKING"
    )


def _executed_imports(tree: ast.Module) -> list[tuple[int, str]]:
    """Imports at any scope that actually EXECUTE — everything except those
    nested under an ``if TYPE_CHECKING:`` guard."""
    guarded_lines: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.If) and _is_type_checking_guard(node):
            for sub in ast.walk(node):
                if isinstance(sub, (ast.Import, ast.ImportFrom)):
                    guarded_lines.add(sub.lineno)
    return [(ln, mod) for ln, mod in _all_imports(tree) if ln not in guarded_lines]


COMPOSE_EXEMPT = "runtime/compose.py"


def test_runtime_reaches_coordination_only_through_compose() -> None:
    """The assembly-root rule: runtime/compose.py is the ONE runtime file that
    may import coordination (lazily, in function bodies). Everywhere else in
    runtime, a coordination import must never execute — that is the seam that
    keeps the coordination→runtime dependency one-way."""
    violations: list[str] = []
    for path in sorted(SRC.rglob("*.py")):
        if "__pycache__" in path.parts:
            continue
        rel_parts = path.relative_to(SRC).parts
        if rel_parts[0] != "runtime":
            continue
        rel = "/".join(rel_parts)
        if rel == COMPOSE_EXEMPT:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for lineno, imported in _executed_imports(tree):
            if _target_roots(imported) & {"coordination"}:
                violations.append(f"{rel}:{lineno}  runtime -> coordination")
    assert not violations, (
        "runtime must reach coordination only through the compose seam"
        f" ({COMPOSE_EXEMPT}). Executing imports:\n  " + "\n  ".join(violations)
    )


def test_coordination_never_imports_runtime() -> None:
    """The same seam, pinned from the other side — and stricter: coordination
    has NO exempt file. Agent execution reaches the runtime only through the
    RunnerPort (ports/runner.py; RunLoop wires ``ctx.runner``), tool merging
    through tooling, agent descriptions through coordination/describe. An
    ``if TYPE_CHECKING`` Agent annotation carries no runtime dependency and
    stays allowed, same exemption philosophy as everywhere else in this file."""
    violations: list[str] = []
    for path in sorted(SRC.rglob("*.py")):
        if "__pycache__" in path.parts:
            continue
        rel_parts = path.relative_to(SRC).parts
        if rel_parts[0] != "coordination":
            continue
        rel = "/".join(rel_parts)
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for lineno, imported in _executed_imports(tree):
            if _target_roots(imported) & {"runtime"}:
                violations.append(f"{rel}:{lineno}  coordination -> runtime")
    assert not violations, (
        "coordination must reach the runtime only through the RunnerPort "
        "(ports/runner.py). Executing imports:\n  " + "\n  ".join(violations)
    )


def test_layering_contract() -> None:
    violations: list[str] = []
    for path in sorted(SRC.rglob("*.py")):
        if "__pycache__" in path.parts:
            continue
        if path.name in EXEMPT_MODULES and path.parent == SRC:
            continue
        src_pkg = _source_pkg(path)
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for lineno, imported in _top_level_imports(tree):
            for target in _target_roots(imported):
                forbidden = FORBIDDEN.get(src_pkg or "", frozenset())
                if target in forbidden:
                    rel = path.relative_to(SRC.parent.parent)
                    violations.append(f"{rel}:{lineno}  {src_pkg} -> {target}")
    assert not violations, "Layering violations (module-level imports):\n" + "\n".join(violations)
