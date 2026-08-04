"""In-process dead-letter queue."""

from __future__ import annotations

import logging
import time
from typing import Any, Literal

logger = logging.getLogger(__name__)

_CLEANUP_INTERVAL_DIVISOR = 10


class InMemoryDeadLetterQueue:
    """Tracks retry counts per message_id and parks terminal failures."""

    def __init__(self, max_retries: int = 3, ttl_seconds: float = 86400.0) -> None:
        self._max_retries = max_retries
        self._ttl = ttl_seconds
        self._counts: dict[str, int] = {}
        self._dead: dict[str, dict[str, Any]] = {}
        self._timestamps: dict[str, float] = {}
        self._last_cleanup = time.monotonic()

    def on_failure(
        self, message_id: str, payload: dict[str, Any], error: str
    ) -> Literal["dead_letter", "retry"]:
        self._maybe_cleanup()
        count = self._counts.get(message_id, 0) + 1
        self._counts[message_id] = count
        self._timestamps[message_id] = time.monotonic()
        if count >= self._max_retries:
            self._dead[message_id] = {"payload": payload, "error": error, "attempts": count}
            self._counts.pop(message_id, None)
            logger.warning(
                "DeadLetterQueue: message %s moved to dead_letter after %d attempts (%s)",
                message_id[:8],
                count,
                error[:80],
            )
            return "dead_letter"
        logger.info(
            "DeadLetterQueue: message %s retry %d/%d (%s)",
            message_id[:8],
            count,
            self._max_retries,
            error[:80],
        )
        return "retry"

    def dead_letters(self) -> list[dict[str, Any]]:
        return list(self._dead.values())

    def _maybe_cleanup(self) -> None:
        now = time.monotonic()
        if now - self._last_cleanup < self._ttl / _CLEANUP_INTERVAL_DIVISOR:
            return
        cutoff = now - self._ttl
        for mid in [m for m, ts in self._timestamps.items() if ts < cutoff]:
            self._counts.pop(mid, None)
            self._timestamps.pop(mid, None)
            self._dead.pop(mid, None)
        self._last_cleanup = now
