"""DeadLetterStore port — terminal sink for contract-violating child results."""

from __future__ import annotations

from typing import Any, Literal, Protocol, runtime_checkable


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
