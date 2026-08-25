"""Redis-backed ``CacheStore``."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

from prodagent.backends.redis.keys import namespaced_key
from prodagent.kernel.types import LLMResponse

if TYPE_CHECKING:
    from redis.asyncio import Redis

__all__ = ["RedisCache"]


class RedisCache:
    """Distributed LLM response cache. One Redis instance serves many processes.

    Entries carry a TTL (default 7 days): unlike the in-memory LRU, a shared
    Redis has no size bound, and cached responses go stale as models and
    pricing change. Pass ``ttl_seconds=None`` only with an external
    eviction policy in place."""

    _DEFAULT_TTL_SECONDS = 7 * 86400

    def __init__(
        self,
        client: Redis,
        *,
        namespace: str = "default",
        ttl_seconds: float | None = _DEFAULT_TTL_SECONDS,
    ) -> None:
        self._client = client
        self._ns = namespace
        self._ttl = int(ttl_seconds) if ttl_seconds is not None else None

    def _key(self, key: str) -> str:
        return namespaced_key(self._ns, "cache", key)

    async def get(self, key: str) -> LLMResponse | None:
        blob = await self._client.get(self._key(key))
        if blob is None:
            return None
        if isinstance(blob, bytes):
            blob = blob.decode()
        return LLMResponse.from_dict(json.loads(blob))

    async def set(self, key: str, response: LLMResponse) -> None:
        await self._client.set(
            self._key(key),
            json.dumps(response.to_dict(), ensure_ascii=False),
            ex=self._ttl,
        )
