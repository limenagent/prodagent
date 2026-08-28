"""Idempotent message suppression for inter-agent handoffs."""

from __future__ import annotations

import asyncio
import logging
import time

logger = logging.getLogger(__name__)


class IdempotentMessageHandler:
    """Seen-id set with a TTL — the plane's replay suppression.

    Check-and-remember under one lock: a message is a duplicate exactly when
    it was admitted before. The TTL bounds memory without a timer thread —
    expired ids are pruned opportunistically on each check."""

    def __init__(self, ttl_seconds: float = 3600.0) -> None:
        self._seen: dict[str, float] = {}
        self._ttl = ttl_seconds
        self._lock = asyncio.Lock()

    async def is_duplicate(self, message_id: str) -> bool:
        async with self._lock:
            now = time.monotonic()
            cutoff = now - self._ttl
            if any(ts < cutoff for ts in self._seen.values()):
                self._seen = {mid: ts for mid, ts in self._seen.items() if ts >= cutoff}
            if message_id in self._seen:
                logger.debug("Duplicate message suppressed: %s", message_id)
                return True
            self._seen[message_id] = now
            return False
