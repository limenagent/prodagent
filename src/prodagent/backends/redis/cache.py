"""Redis-backed ``CacheStore``."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

from prodagent.backends.redis.keys import namespaced_key
from prodagent.core.types import LLMResponse

if TYPE_CHECKING:
    from redis.asyncio import Redis

__all__ = ["RedisCache"]


class RedisCache:
    """Distributed LLM response cache. One Redis instance serves many processes."""

    def __init__(self, client: Redis, *, namespace: str = "default") -> None:
        self._client = client
        self._ns = namespace

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
        await self._client.set(self._key(key), json.dumps(response.to_dict(), ensure_ascii=False))
