"""Redis-backed ``DeadLetterStore``."""

from __future__ import annotations

import asyncio
import json
import time
from typing import TYPE_CHECKING, Any, Literal

from prodagent.backends.redis.keys import namespaced_key

if TYPE_CHECKING:
    from redis import Redis  # sync client

__all__ = ["RedisDeadLetterQueue"]


class RedisDeadLetterQueue:
    """Distributed dead-letter queue with atomic per-message retry counting."""

    def __init__(
        self,
        client: Redis,
        *,
        namespace: str = "default",
        max_retries: int = 3,
        ttl_seconds: float = 86400.0,
    ) -> None:
        self._client = client
        self._ns = namespace
        self._max_retries = max_retries
        self._ttl = ttl_seconds

    def _dead_key(self) -> str:
        return namespaced_key(self._ns, "dlq", "dead")

    async def on_failure(
        self,
        message_id: str,
        payload: dict[str, Any],
        error: str,
    ) -> Literal["dead_letter", "retry"]:
        # sync client — keep off the event loop
        return await asyncio.to_thread(self._on_failure_sync, message_id, payload, error)

    def _on_failure_sync(
        self,
        message_id: str,
        payload: dict[str, Any],
        error: str,
    ) -> Literal["dead_letter", "retry"]:
        count_key = namespaced_key(self._ns, "dlq", "counts", message_id)
        count = self._client.incr(count_key)
        if count == 1:
            self._client.expire(count_key, max(1, int(self._ttl)))
        if count >= self._max_retries:
            record = json.dumps(
                {
                    "message_id": message_id,
                    "payload": payload,
                    "error": error,
                    "attempts": count,
                    "dead_at": time.time(),
                }
            )
            pipe = self._client.pipeline()
            pipe.rpush(self._dead_key(), record)
            pipe.delete(count_key)
            pipe.execute()
            return "dead_letter"
        return "retry"

    async def dead_letters(self) -> list[dict[str, Any]]:
        return await asyncio.to_thread(self._dead_letters_sync)

    def _dead_letters_sync(self) -> list[dict[str, Any]]:
        raw = self._client.lrange(self._dead_key(), 0, -1)
        out = []
        for item in raw:
            if isinstance(item, bytes):
                item = item.decode()
            out.append(json.loads(item))
        return out
