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

Why this size and not smaller: kernel (7 — types/state/budget/bus/step/loop
plus progress, the dead-loop guard; events merged into types and the bus's
dispatch plumbing into bus — one concept, one module), core (7 — config,
errors [the merged error model], event_log, retry, session, types, lazy),
ports (15 protocol files that ARE the kernel's vocabulary; #14 Transport and
#15 BudgetLedgerPort joined in the G0 pre-work: annotation-only contracts,
the message plane's in-process implementation stays behind the compose
seam), tooling (8 — the @tool/dispatch spine), plus runtime core
(agent/config/runner/factory/compose/parent_runtime).
The peer relay moved to coordination/relay.py behind the compose seam, so
the messaging plane left this chain entirely — spawn/peers, the stage
primitives AND the relay load lazily via compose. Importing an Agent pulls
no multi-agent machinery at all.
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
        "prodagent.base",
        "prodagent.base.codec",
        "prodagent.base.config",
        "prodagent.base.determinism",
        "prodagent.base.errors",
        "prodagent.base.event_log",
        "prodagent.base.lazy",
        "prodagent.base.retry",
        "prodagent.base.session",
        "prodagent.base.types",
        "prodagent.kernel",
        "prodagent.kernel.budget",
        "prodagent.kernel.bus",
        "prodagent.kernel.loop",
        "prodagent.kernel.progress",
        "prodagent.kernel.state",
        "prodagent.kernel.step",
        "prodagent.kernel.types",
        "prodagent.ports",
        "prodagent.ports.activation",
        "prodagent.ports.agent_events",
        "prodagent.ports.agent_spec",
        "prodagent.ports.approval",
        "prodagent.ports.budget_ledger",
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
        "prodagent.ports.runner",
        "prodagent.ports.session",
        "prodagent.ports.span",
        "prodagent.ports.tool",
        "prodagent.ports.transport",
        "prodagent.runtime",
        "prodagent.runtime.agent",
        "prodagent.runtime.compose",
        "prodagent.runtime.config",
        "prodagent.runtime.factory",
        "prodagent.runtime.parent_runtime",
        "prodagent.runtime.runner",
        "prodagent.tooling",
        "prodagent.tooling.base",
        "prodagent.tooling.decorator",
        "prodagent.tooling.dispatcher",
        "prodagent.tooling.merge",
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
    assert set(modules) <= {"prodagent", "prodagent.base", "prodagent.base.lazy"}, (
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
