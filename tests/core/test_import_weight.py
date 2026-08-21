"""Import weight — falsifiable evidence for the kernel boundary.

Imports in a clean subprocess and inspects ``sys.modules`` for prodagent.*:

1. ``import prodagent`` loads (almost) no submodules — the top-level
   ``__init__`` is a lazy ``__getattr__``;
2. ``import prodagent.runtime.agent`` (the kernel's main entry) must not
   pull in backends / playground / repl / evaluation — the kernel/optional
   separation. evaluation is only allowed to load later, via the learning
   bundle at runtime.

These are the CI pins for "lightweight": whoever hangs a heavy module on
the kernel import chain turns this red first.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

SRC_PARENT = Path(__file__).resolve().parents[2] / "src"

# optional pieces that must stay out of the kernel import chain
FORBIDDEN_AFTER_AGENT_IMPORT = ("prodagent.backends", "prodagent.playground",
                                "prodagent.repl", "prodagent.evaluation")

PROBE = (
    "import sys; {import_line}; "
    "print('\\n'.join(sorted(m for m in sys.modules if m.startswith('prodagent'))))"
)


def _loaded_modules(import_line: str) -> list[str]:
    env = {**os.environ, "PYTHONPATH": str(SRC_PARENT)}
    proc = subprocess.run(
        [sys.executable, "-c", PROBE.format(import_line=import_line)],
        capture_output=True, text=True, check=True, env=env,
    )
    return [line for line in proc.stdout.splitlines() if line.strip()]


def test_import_prodagent_loads_almost_nothing() -> None:
    modules = _loaded_modules("import prodagent")
    # The top-level __init__ is lazy: only the package itself may load.
    assert modules == ["prodagent"], (
        f"`import prodagent` should load zero submodules (lazy __init__), got: {modules}"
    )


def test_agent_import_keeps_kernel_isolated() -> None:
    modules = _loaded_modules("import prodagent.runtime.agent")
    leaked = [
        m for m in modules
        if m.startswith(FORBIDDEN_AFTER_AGENT_IMPORT)
    ]
    assert not leaked, (
        f"the kernel entry leaked optional packages into the import chain: {leaked}\n"
        f"all loaded: {modules}"
    )
