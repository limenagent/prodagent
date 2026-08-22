"""Import weight — falsifiable evidence for the kernel boundary.

Imports in a clean subprocess and inspects ``sys.modules`` for prodagent.*:

1. ``import prodagent`` loads (almost) no submodules — the top-level
   ``__init__`` is a lazy ``__getattr__``;
2. ``import prodagent.runtime.agent`` (the kernel's main entry) loads
   EXACTLY ``KERNEL_EXPECTED`` — an explicit allowlist, so both additions
   and removals are conscious decisions, not drift;
3. optional heavy packages (backends / playground / repl / evaluation /
   cognition / mcp / guardrail) never appear on the chain.

These are the CI pins for "lightweight": whoever hangs a heavy module on
the kernel import chain turns this red first.

Why 65 and not fewer: the remaining mass is coordination + messaging
(13 — spawn/peers/r_loop are the multi-agent kernel, kept by owner
decision), ports (15 — tiny Protocol files that ARE the kernel's
vocabulary), tooling (10 — the @tool/dispatch/runner spine), and core
state/budget/events. Going lower would mean splitting ports or demoting
coordination — cost exceeds value.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

SRC_PARENT = Path(__file__).resolve().parents[2] / "src"

# optional pieces that must stay off the kernel import chain entirely
FORBIDDEN_AFTER_AGENT_IMPORT = (
    "prodagent.backends",
    "prodagent.playground",
    "prodagent.cognition",
    "prodagent.mcp",
    "prodagent.skills",
)

# exact module set loaded by `import prodagent.runtime.agent`
KERNEL_EXPECTED = frozenset(
    {
        "prodagent",
        "prodagent.core",
    "prodagent.core.lazy",
        "prodagent.core.budget",
        "prodagent.core.config",
        "prodagent.core.error_classifier",
        "prodagent.core.error_reason",
        "prodagent.core.events",
        "prodagent.core.exceptions",
        "prodagent.core.progress",
        "prodagent.core.state",
        "prodagent.core.state.run",
        "prodagent.core.state.session",
        "prodagent.core.types",
        "prodagent.hooks",
        "prodagent.hooks.events",
        "prodagent.hooks.gates",
        "prodagent.hooks.registry",
        "prodagent.llm",
        "prodagent.ports",
        "prodagent.ports.approval",
        "prodagent.ports.cache",
        "prodagent.ports.checkpoint",
        "prodagent.ports.dead_letter",
        "prodagent.ports.document",
        "prodagent.ports.event_log",
        "prodagent.ports.experience",
        "prodagent.ports.graph",
        "prodagent.ports.leaf_executor",
        "prodagent.ports.llm",
        "prodagent.ports.lock",
        "prodagent.ports.session",
        "prodagent.ports.span",
        "prodagent.ports.tool",
        "prodagent.tooling.retry",
        "prodagent.runtime",
        "prodagent.runtime._tool_merge",
        "prodagent.runtime.agent",
        "prodagent.runtime.config",
        "prodagent.coordination",
        "prodagent.coordination.accounting",
        "prodagent.coordination.budget_ledger",
        "prodagent.coordination.messaging",
        "prodagent.coordination.messaging.contract",
        "prodagent.coordination.messaging.envelope",
        "prodagent.coordination.messaging.idempotency",
        "prodagent.coordination.messaging.interceptors",
        "prodagent.coordination.messaging.packet",
        "prodagent.coordination.messaging.pipeline",
        "prodagent.coordination.parent_runtime",
        "prodagent.coordination.peer",
        "prodagent.coordination.run_loop",
        "prodagent.coordination.spawn",
        "prodagent.runtime.factory",
        "prodagent.runtime.reactive",
        "prodagent.tooling",
        "prodagent.tooling.base",
        "prodagent.tooling.decorator",
        "prodagent.tooling.dispatcher",
        "prodagent.tooling.registry",
        "prodagent.tooling.reliability",
        "prodagent.tooling.reliability.circuit_breaker",
        "prodagent.tooling.search",
        "prodagent.tooling.skill_resolver",
    }
)

PROBE = (
    "import sys; {import_line}; "
    "print('\\n'.join(sorted(m for m in sys.modules if m.startswith('prodagent'))))"
)


def _loaded_modules(import_line: str) -> list[str]:
    env = {**os.environ, "PYTHONPATH": str(SRC_PARENT)}
    proc = subprocess.run(
        [sys.executable, "-c", PROBE.format(import_line=import_line)],
        capture_output=True,
        text=True,
        check=True,
        env=env,
    )
    return [line for line in proc.stdout.splitlines() if line.strip()]


def test_import_prodagent_loads_almost_nothing() -> None:
    modules = _loaded_modules("import prodagent")
    # Lazy __init__: the package plus the tiny shared lazy-loading helper only.
    assert set(modules) <= {"prodagent", "prodagent.core", "prodagent.core.lazy"}, (
        f"`import prodagent` should load no submodules beyond core.lazy, got: {modules}"
    )


def test_agent_import_matches_kernel_allowlist() -> None:
    loaded = frozenset(_loaded_modules("import prodagent.runtime.agent"))
    added = sorted(loaded - KERNEL_EXPECTED)
    removed = sorted(KERNEL_EXPECTED - loaded)
    assert not added and not removed, (
        "Kernel import set changed — update KERNEL_EXPECTED consciously:\n"
        f"  added:   {added}\n"
        f"  removed: {removed}\n"
        f"  loaded {len(loaded)} modules, allowlist has {len(KERNEL_EXPECTED)}"
    )


def test_agent_import_keeps_optional_packages_out() -> None:
    loaded = _loaded_modules("import prodagent.runtime.agent")
    leaked = [m for m in loaded if m.startswith(FORBIDDEN_AFTER_AGENT_IMPORT)]
    assert not leaked, f"the kernel entry leaked optional packages into the import chain: {leaked}"
