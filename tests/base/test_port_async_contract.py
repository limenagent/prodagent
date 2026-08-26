"""Architectural invariant: every store port is async.

The Protocol ports are the framework's contract surface. A sync method on
a network-backed implementation (Redis, Neo4j, Postgres) would block
the event loop from inside the agent's async runtime — this suite pins the
convention so a new port (or a edit to an old one) cannot quietly regress it.
"""

from __future__ import annotations

import inspect

from prodagent.ports import (
    ApprovalStore,
    CacheStore,
    CheckpointStore,
    DeadLetterStore,
    DocumentStore,
    EventLog,
    ExperienceStore,
    GraphStore,
    LLMClient,
    LockStore,
    SessionStore,
    SpanExporter,
    Tool,
    Transport,
)

STORE_PORTS = [
    ApprovalStore,
    CacheStore,
    CheckpointStore,
    DeadLetterStore,
    DocumentStore,
    EventLog,
    ExperienceStore,
    GraphStore,
    LLMClient,
    LockStore,
    SessionStore,
    SpanExporter,
    Tool,
    Transport,
]


def test_every_store_port_method_is_async() -> None:
    offenders: list[str] = []
    for protocol in STORE_PORTS:
        for name, member in inspect.getmembers(protocol):
            if name.startswith("_") and name != "__call__":
                continue
            if isinstance(member, property):
                continue  # synchronous attributes are fine — they cannot do I/O
            if not callable(member):
                continue
            if not (
                inspect.iscoroutinefunction(member)
                # a plain `def` returning an AsyncGenerator is equally non-blocking
                or inspect.isasyncgenfunction(member)
            ):
                offenders.append(f"{protocol.__name__}.{name}")
    assert not offenders, f"sync I/O surface on store ports: {offenders}"


def test_port_count_is_stable() -> None:
    """Changing the port roster is a deliberate act — update this count and
    the suite above in the same commit. Transport joined as #14 (G0 seam);
    BudgetLedgerPort (#15) is deliberately not here — it is a coordination
    port with sync read views, not an I/O store, and has its own conformance
    suite in tests/runtime/test_transport_and_ledger_ports.py."""
    assert len(STORE_PORTS) == 14, (
        "store-port roster changed; update test_every_store_port_method_is_async "
        "and this count together"
    )
