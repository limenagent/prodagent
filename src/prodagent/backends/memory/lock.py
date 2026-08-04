"""In-process ``LockStore`` — ``asyncio.Lock`` per name, single-host."""

from __future__ import annotations

import asyncio
import contextlib
import logging
from dataclasses import dataclass

from prodagent.ports.lock import LockToken

logger = logging.getLogger(__name__)


@dataclass
class _Handle:
    lock: asyncio.Lock
    holder: asyncio.Task[object] | None = None


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

        try:
            await asyncio.wait_for(handle.lock.acquire(), timeout=timeout)
        except TimeoutError as exc:
            raise TimeoutError(
                f"InProcessLockStore: timed out acquiring lock {name!r} after {timeout}s"
            ) from exc
        handle.holder = asyncio.current_task()
        return LockToken(name=name, handle=handle)

    async def release(self, token: LockToken) -> None:
        handle: _Handle = token.handle  # type: ignore[assignment]
        if handle.holder is not asyncio.current_task():
            return  # Only the holder may release; idempotent otherwise.
        handle.holder = None
        with contextlib.suppress(RuntimeError):
            handle.lock.release()

    async def extend(self, token: LockToken, *, ttl: float) -> None:
        # In-process locks have no TTL to extend.
        return None
