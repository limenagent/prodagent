"""The package tree IS the book's table of contents.

Thirteen packages in learning order — vocabulary → contracts → providers →
tools → the agent → planning → collaboration → cognition → observation →
skills → storage → protocol bridging → the playground. A new top-level
package is a chapter decision, not an accident: this test fails first.
"""

from __future__ import annotations

from pathlib import Path

SRC = Path(__file__).resolve().parents[2] / "src" / "prodagent"

# learning order — the order a reader meets them
BOOK_TOC = [
    "core",        # vocabulary: types, state, budget, errors, config
    "ports",       # the contracts
    "llm",         # providers, fake, cache, pricing
    "tooling",     # @tool, dispatch, registry + breaker, skills loader
    "runtime",     # the Agent and the REACTIVE leaf loop
    "plan",        # PLAN_FIRST: DAG, planner, executor, workflow
    "coordination",# multi-agent: spawn/peers/ensemble/blackboard/work_queue + messaging plane
    "cognition",   # context compression + long-term memory
    "hooks",       # the tri-protocol bus, bundles, approval, spans, console
    "skills",      # runbook registry + the learning loop
    "backends",    # port implementations + the resolution factory
    "mcp",         # external tool bridging
    "playground",  # the visual cockpit (app-tier)
]


def test_package_tree_matches_the_book() -> None:
    actual = sorted(p.name for p in SRC.iterdir() if p.is_dir() and p.name != "__pycache__")
    assert actual == sorted(BOOK_TOC), (
        f"top-level package set drifted from the book's TOC:\n"
        f"  expected: {sorted(BOOK_TOC)}\n  actual:   {actual}\n"
        f"adding or removing a package is a chapter decision — update BOOK_TOC "
        f"consciously and in the same commit as the README architecture section."
    )


def test_no_stray_top_level_modules() -> None:
    """Only the entry point may live at the top level beside the packages."""
    files = sorted(p.name for p in SRC.glob("*.py"))
    assert files == ["__init__.py", "__main__.py", "py.typed"] or files == [
        "__init__.py",
        "__main__.py",
    ], f"stray top-level files: {files}"
