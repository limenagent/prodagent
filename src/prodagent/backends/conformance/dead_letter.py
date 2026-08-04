"""Conformance tests for ``DeadLetterStore`` implementations."""

from __future__ import annotations

from collections.abc import Callable
from typing import TypeAlias

from prodagent.ports.dead_letter import DeadLetterStore

Factory: TypeAlias = Callable[[], DeadLetterStore]


def run_dead_letter_conformance(make_store: Factory) -> None:
    store = make_store()

    result = store.on_failure("m1", {"x": 1}, "boom")
    assert result == "retry", "first failure within retry budget returns retry"


def run_dead_letter_escalation_conformance(make_store: Factory) -> None:
    """After ``max_retries`` attempts, the message is parked as dead_letter."""
    store = make_store()
    max_retries = getattr(store, "_max_retries", 3)

    results = [store.on_failure("m2", {"x": 1}, "boom") for _ in range(max_retries)]
    assert all(r == "retry" for r in results[:-1])
    assert results[-1] == "dead_letter", "final attempt parks the message"

    dead = store.dead_letters()
    assert any(d.get("payload", {}).get("x") == 1 for d in dead)


def run_dead_letter_message_isolation_conformance(make_store: Factory) -> None:
    """Retry counts are per-message_id — one message's failures don't bleed into another."""
    store = make_store()
    store.on_failure("a", {}, "e1")
    store.on_failure("b", {}, "e2")
    store.on_failure("a", {}, "e1")

    # 'a' has 2 attempts, 'b' has 1 — neither should be dead-lettered yet
    dead = store.dead_letters()
    assert dead == [], "no message should have hit max_retries yet"
