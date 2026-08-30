"""In-process ``LockStore`` — ``asyncio.Lock`` per name, single-host."""

from __future__ import annotations

import asyncio
import contextlib
import logging
from dataclasses import dataclass
from typing import Any, cast

from prodagent.ports.messaging import LockToken

logger = logging.getLogger(__name__)


@dataclass
class _Handle:
    lock: asyncio.Lock
    ticket: object | None = None


class InProcessLockStore:
    """Per-name ``asyncio.Lock`` registry."""

    def __init__(self) -> None:
        self._locks: dict[str, _Handle] = {}
        self._registry_lock = asyncio.Lock()

    async def acquire(self, name: str, *, timeout: float) -> LockToken:
        async with self._registry_lock:
            handle = self._locks.get(name)
            if handle is None:
                handle = _Handle(lock=asyncio.Lock())
                self._locks[name] = handle

        if timeout <= 0:
            # asyncio.wait_for(coro, timeout=0) always fails here, even on a
            # free lock: wait_for wraps the acquire in a new Task, and its
            # zero-second cancellation callback races the task's very first
            # scheduling — the cancellation wins every time. That makes
            # timeout=0 (the documented "try once, don't block" case) report
            # a false timeout on an uncontended lock. Handle it as a direct
            # non-blocking check instead: nothing can run between
            # ``locked()`` and the acquire call below (no ``await`` in
            # between), so this is race-free under asyncio's cooperative
            # scheduling.
            if handle.lock.locked():
                raise TimeoutError(
                    f"InProcessLockStore: lock {name!r} already held (non-blocking acquire)"
                )
            await handle.lock.acquire()
        else:
            try:
                await asyncio.wait_for(handle.lock.acquire(), timeout=timeout)
            except TimeoutError as exc:
                raise TimeoutError(
                    f"InProcessLockStore: timed out acquiring lock {name!r} after {timeout}s"
                ) from exc
        # A fresh per-acquisition ticket, not the acquiring task: callers
        # commonly acquire in one coroutine/task and release from another
        # (e.g. "race for the lock, then a different call computes and
        # releases it"), so ownership can't be tied to task identity.
        # Mirrors RedisLockStore's token-verified release — the ticket in
        # hand is the sole proof of ownership, same as the value it stores
        # in the Redis key.
        ticket = object()
        handle.ticket = ticket
        return LockToken(name=name, handle=(handle, ticket))

    async def release(self, token: LockToken) -> None:
        handle, ticket = cast("tuple[_Handle, Any]", token.handle)
        if handle.ticket is not ticket:
            return  # Stale or already-released token — idempotent no-op.
        handle.ticket = None
        with contextlib.suppress(RuntimeError):
            handle.lock.release()

    async def extend(self, token: LockToken, *, ttl: float) -> None:
        # In-process locks have no TTL to extend.
        return None
