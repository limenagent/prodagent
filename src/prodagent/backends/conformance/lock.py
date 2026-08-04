"""Conformance tests for ``LockStore`` implementations."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import TypeAlias

from prodagent.ports.lock import LockStore

Factory: TypeAlias = Callable[[], LockStore]


async def run_lock_conformance(make_store: Factory) -> None:
    store = make_store()

    token = await store.acquire("resource", timeout=1.0)
    assert token.name == "resource"
    await store.release(token)


async def run_lock_mutual_exclusion_conformance(make_store: Factory) -> None:
    """A second acquire on a held lock times out, not returns a stale token."""
    store = make_store()
    held = await store.acquire("mux", timeout=1.0)

    try:
        await asyncio.wait_for(store.acquire("mux", timeout=0.05), timeout=1.0)
    except TimeoutError:
        pass
    else:
        raise AssertionError("second acquire on held lock should have timed out")

    await store.release(held)
    token = await store.acquire("mux", timeout=1.0)
    assert token.name == "mux"
    await store.release(token)


async def run_lock_release_idempotent_conformance(make_store: Factory) -> None:
    """Releasing an already-released lock is a no-op, not an error."""
    store = make_store()
    token = await store.acquire("idem", timeout=1.0)
    await store.release(token)
    await store.release(token)
