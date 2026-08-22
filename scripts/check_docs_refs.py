#!/usr/bin/env python3
"""Check that every code-file reference in docs/ resolves to a real file.

Docs cite code as `src/prodagent/.../x.py:123`, `tests/core/test_x.py`, or a
repo-relative `package/module.py` inside backticks. A stale reference turns a
teaching doc into a lie; this script fails CI before that ships.

Usage: python scripts/check_docs_refs.py [--docs-dir docs]
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]

# explicit repo-rooted paths
ROOTED = re.compile(r"(?:src/prodagent|tests|examples|scripts)/[\w/]+\.py")
# backticked package-relative paths like `coordination/run_loop.py`
BACKTICKED = re.compile(r"`((?:core|ports|llm|tooling|runtime|plan|coordination|cognition|hooks|skills|backends|mcp|playground)/[\w/]+\.py)`")
# the 13-package guard: a backticked path whose first segment is not a package
PACKAGES = {
    "core", "ports", "llm", "tooling", "runtime", "plan", "coordination",
    "cognition", "hooks", "skills", "backends", "mcp", "playground",
}


def main() -> int:
    docs_dir = Path(sys.argv[sys.argv.index("--docs-dir") + 1]) if "--docs-dir" in sys.argv else REPO / "docs"
    missing: list[tuple[Path, str]] = []
    checked = 0
    for md in sorted(docs_dir.rglob("*.md")):
        text = md.read_text(encoding="utf-8")
        # strip fenced code blocks other than mermaid? keep them — paths inside
        # prose code (`pip install ...` etc.) never match the .py patterns.
        refs: set[str] = set(ROOTED.findall(text))
        refs.update(BACKTICKED.findall(text))
        for ref in refs:
            checked += 1
            candidates = [REPO / ref, REPO / "src" / "prodagent" / ref]
            if not any(c.exists() for c in candidates):
                missing.append((md, ref))
    if missing:
        for md, ref in missing:
            print(f"MISSING {ref}  (referenced in {md.relative_to(REPO)})")
        print(f"\n{len(missing)} stale code reference(s); {checked} checked.")
        return 1
    print(f"docs code references: {checked} checked, all resolve.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
