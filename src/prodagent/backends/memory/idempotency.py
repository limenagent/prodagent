"""In-process ``IdempotencyStore`` — dict-backed, single-host."""

from __future__ import annotations

import asyncio
import logging
import time

logger = logging.getLogger(__name__)


class InMemoryIdempotencyStore:
    """Dict-backed idempotency store with TTL expiry."""

    def __init__(self) -> None:
        self._seen: dict[str, float] = {}
        self._lock = asyncio.Lock()

    async def check_and_mark(self, key: str, *, ttl_seconds: float) -> bool:
        async with self._lock:
            now = time.monotonic()
            cutoff = now - ttl_seconds
            # Lazy GC: drop expired entries that we happen to touch.
            if any(ts < cutoff for ts in self._seen.values()):
                self._seen = {k: ts for k, ts in self._seen.items() if ts >= cutoff}
            if key in self._seen:
                logger.debug("IdempotencyStore: duplicate suppressed: %s", key)
                return False
            self._seen[key] = now
            return True
