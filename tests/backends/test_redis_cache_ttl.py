"""RedisCache SET must carry a TTL — a shared Redis has no size bound."""

from __future__ import annotations

from typing import Any

from prodagent.backends.redis.cache import RedisCache
from prodagent.kernel.types import LLMResponse

_RESPONSE = LLMResponse(content="hi", stop_reason="end_turn", input_tokens=1, output_tokens=1)


class _StubRedis:
    def __init__(self) -> None:
        self.set_calls: list[dict[str, Any]] = []

    async def set(self, key: str, value: str, ex: int | None = None) -> None:
        self.set_calls.append({"key": key, "value": value, "ex": ex})


async def test_set_carries_default_ttl():
    client = _StubRedis()
    cache = RedisCache(client, namespace="t")
    await cache.set("k1", _RESPONSE)
    assert client.set_calls[0]["ex"] == 7 * 86400


async def test_ttl_is_configurable_and_disableable():
    client = _StubRedis()
    await RedisCache(client, namespace="t", ttl_seconds=60).set("k1", _RESPONSE)
    assert client.set_calls[0]["ex"] == 60

    await RedisCache(client, namespace="t", ttl_seconds=None).set("k2", _RESPONSE)
    assert client.set_calls[1]["ex"] is None
