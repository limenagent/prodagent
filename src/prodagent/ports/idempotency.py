"""IdempotencyStore port — atomic check-and-mark for duplicate suppression."""

from __future__ import annotations

from typing import Protocol, runtime_checkable


@runtime_checkable
class IdempotencyStore(Protocol):
    """Atomic duplicate-suppression store.

    Multi-replica contract: ``check_and_mark`` must be a single atomic CAS —
    Redis ``SET NX``, Postgres ``INSERT ... ON CONFLICT DO NOTHING``, etc. A
    non-atomic read-then-write defeats the purpose under concurrency.
    """

    async def check_and_mark(self, key: str, *, ttl_seconds: float) -> bool:
        """Atomically mark ``key`` as seen.

        Returns ``True`` if this is the first time ``key`` is observed
        (caller proceeds), ``False`` if ``key`` was already seen within its
        TTL window (caller suppresses).
        """
        ...
