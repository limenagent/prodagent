"""LockStore port — named distributed lock primitive.

Other ports (CheckpointStore.save with expected_version, EventLog.append with
expected_seq, ApprovalStore.submit_decision) rely on this for mutual exclusion
across processes. Exposed publicly so user code protecting custom critical
sections can use the same backend.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, runtime_checkable


@dataclass(frozen=True)
class LockToken:
    """Opaque handle returned by ``acquire``, required by ``release``/``extend``.

    Implementations may embed any backend-specific data (Redis lock value,
    Postgres advisory lock id, etc.) — callers must treat it as opaque.
    """

    name: str
    handle: object


@runtime_checkable
class LockStore(Protocol):
    """Named distributed lock primitive.

    Capabilities:
      BASE (required): acquire, release
      EXTENDED (optional): extend
    """

    async def acquire(self, name: str, *, timeout: float) -> LockToken:
        """Block until ``name`` is acquired or ``timeout`` seconds elapse.

        Raises on timeout — never returns a token that is not held.
        """
        ...

    async def release(self, token: LockToken) -> None:
        """Release the lock. Must be idempotent — releasing an expired or
        already-released lock is a no-op (or best-effort)."""
        ...

    async def extend(self, token: LockToken, *, ttl: float) -> None:
        """Extend the held lock's TTL by ``ttl`` seconds.

        Implementations may raise ``NotImplementedError`` if the backend
        does not support extension.
        """
        ...
