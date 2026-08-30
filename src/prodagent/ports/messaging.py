"""Message-plane ports — crossing the boundary, parking failures, exclusion.

Family home for the book's messaging-and-collaboration socket family (transport / dead_letter /
lock, merged 2026-08): ``Transport`` is the one-method seam a distributed
message plane plugs into; ``DeadLetterStore`` is the terminal sink for
contract-violating child results and owns the retry budget; ``LockStore`` is
the named distributed lock the optimistic-concurrency ports rely on across
processes.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Literal, Protocol, runtime_checkable

if TYPE_CHECKING:
    from prodagent.coordination.messaging.envelope import Crossing, Delivery

# ════════════ from transport.py ════════════

@runtime_checkable
class Transport(Protocol):
    """One boundary direction of one primitive's message plane.

    ``send`` is the wire boundary: everything before it is caller-side
    (minting the crossing), everything after it is plane-side (interceptors,
    delivery verdict). Implementations must preserve pipeline semantics —
    in-order slots, dead-letter-once on strict rejection, duplicate
    short-circuit — because callers translate :class:`Delivery` statuses
    into control-flow decisions (a spawn dies pre-flight on a rejected
    dispatch; a chain stops on a duplicate relay).
    """

    async def send(self, crossing: Crossing[Any]) -> Delivery[Any]: ...


# ════════════ from dead_letter.py ════════════

@runtime_checkable
class DeadLetterStore(Protocol):
    """Persistence port — swap for Redis/DB in multi-process deployments.

    Async like every other store port: a network-backed implementation must
    never block the event loop from inside the messaging pipeline.
    """

    async def on_failure(
        self,
        message_id: str,
        payload: dict[str, Any],
        error: str,
    ) -> Literal["dead_letter", "retry"]:
        """Record a delivery failure and rule on the message's fate: park it
        (dead_letter) or hand it back for another attempt (retry). The store
        owns the retry budget so callers can't each invent their own."""
        ...

    async def dead_letters(self) -> list[dict[str, Any]]: ...


# ════════════ from lock.py ════════════

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
