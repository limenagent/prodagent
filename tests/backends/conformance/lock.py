"""Conformance tests for ``LockStore`` implementations."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import TypeAlias

from prodagent.ports.persistence import LockStore

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


async def run_lock_nonblocking_tryacquire_conformance(make_store: Factory) -> None:
    """``timeout=0`` is a true non-blocking try — succeeds on a free lock,
    fails immediately (not "eventually") on a held one.

    Regression coverage for a real bug in ``InProcessLockStore``: wrapping
    the acquire in ``asyncio.wait_for(..., timeout=0)`` raced its zero-second
    cancellation against the acquire's own scheduling and lost every time,
    so a completely free lock still reported a timeout. Buzz-in arbitration
    depends on ``timeout=0`` being a working trylock.
    """
    store = make_store()

    token = await store.acquire("trylock-free", timeout=0)
    assert token.name == "trylock-free"
    await store.release(token)

    held = await store.acquire("trylock-held", timeout=1.0)
    try:
        await asyncio.wait_for(store.acquire("trylock-held", timeout=0), timeout=0.5)
    except TimeoutError:
        pass
    else:
        raise AssertionError("non-blocking acquire on a held lock should have raised immediately")
    await store.release(held)
