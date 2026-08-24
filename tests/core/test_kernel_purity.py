"""Kernel purity — the kernel imports no capability package at runtime.

Loading any kernel module must not pull in tooling / cognition / hooks /
plan / coordination / mcp / skills / backends / playground / llm. ``core``
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
