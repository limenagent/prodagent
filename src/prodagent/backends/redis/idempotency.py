"""Redis-backed ``IdempotencyStore``."""

from __future__ import annotations

from typing import TYPE_CHECKING

from prodagent.backends.redis.keys import namespaced_key

if TYPE_CHECKING:
    from redis.asyncio import Redis

__all__ = ["RedisIdempotencyStore"]


class RedisIdempotencyStore:
    """Atomic duplicate-suppression across replicas."""

    def __init__(self, client: Redis, *, namespace: str = "default") -> None:
        self._client = client
        self._ns = namespace

    def _key(self, key: str) -> str:
        return namespaced_key(self._ns, "idem", key)

    async def check_and_mark(self, key: str, *, ttl_seconds: float) -> bool:
        # SET key value NX EX ttl  →  True if set (first caller), None if existed
        result = await self._client.set(self._key(key), "1", nx=True, ex=int(max(1, ttl_seconds)))
        return bool(result)
