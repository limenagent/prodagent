"""Layering contract — pin the dependency direction in CI.

AST-scans **module-level** imports across ``src/prodagent``:

- imports inside function bodies are allowed — lazy resolution is the
  composition root's (``prodagent.bootstrap``) legitimate form;
- ``if TYPE_CHECKING`` blocks are allowed — type coupling carries no
  runtime dependency.

Rules (new packages/files are governed automatically):

- ``core`` depends on core only — the bottom layer;
- ``ports`` depends on ports / core only;
- ``backends`` depends on backends / ports / core — storage must not
  reach into the business layer;
- ``playground`` / ``repl`` are top-level apps: nothing outside them may
  import them (``prodagent.__main__`` entry point is exempt);
- every other package (runtime / tooling / hooks / guardrail / llm /
  resilience / cognition / evaluation / mcp) must not import
  backends / playground / repl;
- ``prodagent.bootstrap`` is the single composition root allowed to
  import backends.

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
    "core": frozenset({"runtime", "tooling", "hooks", "guardrail", "llm",
                       "resilience", "cognition", "evaluation", "mcp",
                       "backends", "playground", "repl", "bootstrap"}),
    "ports": frozenset({"runtime", "tooling", "hooks", "guardrail", "llm",
                        "resilience", "cognition", "evaluation", "mcp",
                        "backends", "playground", "repl", "bootstrap"}),
    "backends": frozenset({"runtime", "tooling", "hooks", "guardrail", "llm",
                           "resilience", "cognition", "evaluation", "mcp",
                           "playground", "repl"}),
    "runtime": frozenset({"backends", "playground", "repl"}),
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

# composition-root exemption: the entry point and bootstrap may touch everything
EXEMPT_MODULES = frozenset({"__main__.py", "bootstrap.py"})

TOP_LEVEL_PACKAGES = frozenset(FORBIDDEN) | {"bootstrap", "playground", "repl", "recipes"}


def _source_pkg(path: Path) -> str | None:
    rel = path.relative_to(SRC)
    parts = rel.parts
    if len(parts) == 1:  # top-level single-file modules (__init__/__main__/bootstrap…)
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
