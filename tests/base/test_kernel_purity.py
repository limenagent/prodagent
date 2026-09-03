"""Kernel purity — the kernel imports no capability package at runtime.

Loading any kernel module must not pull in tooling / cognition / hooks /
plan / coordination / mcp / skills / backends / playground / llm. ``base``
(shared mechanics) and ``ports`` (contracts) are the allowed base layers.
TYPE_CHECKING-only references are fine — they never execute.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

SRC_PARENT = Path(__file__).resolve().parents[2] / "src"

FORBIDDEN_IN_KERNEL = (
    "prodagent.tooling",
    "prodagent.cognition",
    "prodagent.hooks",
    "prodagent.plan",
    "prodagent.coordination",
    "prodagent.mcp",
    "prodagent.skills",
    "prodagent.backends",
    "prodagent.playground",
    "prodagent.llm",
)

KERNEL_MODULES = sorted(
    p.stem for p in (SRC_PARENT / "prodagent" / "kernel").glob("*.py") if p.stem != "__init__"
)


def _loaded_by(import_line: str) -> list[str]:
    env = {**os.environ, "PYTHONPATH": str(SRC_PARENT)}
    proc = subprocess.run(
        [
            sys.executable,
            "-c",
            f"import sys; {import_line}; print('\\n'.join(sorted(m for m in sys.modules if m.startswith('prodagent'))))",
        ],
        capture_output=True,
        text=True,
        check=True,
        env=env,
    )
    return [line for line in proc.stdout.splitlines() if line.strip()]


def test_kernel_modules_load_no_capability_package() -> None:
    for mod in KERNEL_MODULES:
        loaded = _loaded_by(f"import prodagent.kernel.{mod}")
        leaked = [m for m in loaded if m.startswith(FORBIDDEN_IN_KERNEL)]
        assert not leaked, (
            f"prodagent.kernel.{mod} pulled capability packages into its import chain: {leaked}"
        )


def test_kernel_does_not_import_plan() -> None:
    """The layering after the unification: the Scheduler lives in ``plan``
    (it orchestrates hooks, models and tools), so the kernel stays pure —
    the reading unit below the blueprint, never above it."""
    loaded = _loaded_by(
        "import prodagent.runtime.recipes.agent_loop, prodagent.runtime.recipes.agent_loop, prodagent.kernel.bodies"
    )
    leaked = [m for m in loaded if m.startswith("prodagent.plan")]
    assert not leaked, f"kernel pulled the plan layer into its import chain: {leaked}"


def test_kernel_has_no_loop_or_engine_vocabulary() -> None:
    """Column 3/23's law, as a gate: the kernel knows bodies, not agents —
    no autonomous kind, no engine slot, no loop machinery. The loop lives
    in runtime/recipes and reaches execution through the generic wiring
    bag (a mapping the kernel carries but never reads)."""
    import pathlib
    import re

    kernel_dir = pathlib.Path("src/prodagent/kernel")
    banned = re.compile(r"autonom", re.IGNORECASE)
    for path in kernel_dir.glob("*.py"):
        text = path.read_text(encoding="utf-8")
        if path.name == "bodies.py":
            # the wire-kind refusal names exactly what it refuses — allowed
            continue
        hits = [ln for ln in text.splitlines() if banned.search(ln)]
        assert not hits, f"{path.name} still speaks autonomy: {hits[:3]}"

    from prodagent.kernel.bodies import NodeKind
    from prodagent.kernel.body import NodeContext

    assert not hasattr(NodeKind, "AUTONOMOUS"), "the autonomous kind left the kernel"
    assert not hasattr(NodeContext, "engine"), "the engine slot left the NodeContext"
    assert "wiring" in NodeContext.__dataclass_fields__, (
        "the generic service bag is how recipe bodies receive collaborators"
    )
    assert pathlib.Path("src/prodagent/runtime/recipes/loop_body.py").exists(), (
        "the loop body lives in the recipes layer now"
    )
