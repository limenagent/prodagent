"""Conformance tests for ``IdempotencyStore`` implementations."""

from __future__ import annotations

from collections.abc import Callable
from typing import TypeAlias

from prodagent.ports.idempotency import IdempotencyStore

Factory: TypeAlias = Callable[[], IdempotencyStore]


async def run_idempotency_conformance(make_store: Factory) -> None:
    store = make_store()

    first = await store.check_and_mark("k1", ttl_seconds=60.0)
    assert first is True, "first observation returns True"

    second = await store.check_and_mark("k1", ttl_seconds=60.0)
    assert second is False, "duplicate within TTL returns False"


async def run_idempotency_key_isolation_conformance(make_store: Factory) -> None:
    store = make_store()
    assert await store.check_and_mark("a", ttl_seconds=60.0) is True
    assert await store.check_and_mark("b", ttl_seconds=60.0) is True
    assert await store.check_and_mark("a", ttl_seconds=60.0) is False
    assert await store.check_and_mark("b", ttl_seconds=60.0) is False


async def run_idempotency_concurrent_conformance(make_store: Factory) -> None:
    """Under concurrency, exactly one caller wins the race for a fresh key."""
    import asyncio

    store = make_store()
    results = await asyncio.gather(
        *[store.check_and_mark("race", ttl_seconds=60.0) for _ in range(8)]
    )
    assert results.count(True) == 1, f"expected 1 winner, got {results.count(True)}"
    assert results.count(False) == 7
