from __future__ import annotations

from prodagent.backends.memory import InMemoryCache
from prodagent.kernel.types import LLMResponse
from prodagent.llm import LLMConfig, noop_chunk
from prodagent.llm.cache import (
    CachingLLMClient,
    cache_key_for,
)


class _CountingLLM:
    def __init__(self) -> None:
        self.calls = 0
        self.last_chunks: list[str] = []

    async def complete(self, messages, *, system="", tools=None, config=None, on_chunk):
        self.calls += 1
        self.last_chunks = []
        if on_chunk is not noop_chunk:
            await on_chunk("hello")
            self.last_chunks.append("hello")
        return LLMResponse(content="hello", input_tokens=1, output_tokens=1, model="fake")


def _make_messages(content: str = "hi") -> list[dict]:
    return [{"role": "user", "content": content}]


class TestCacheKey:
    def test_same_request_same_key(self):
        msgs = _make_messages()
        k1 = cache_key_for(msgs, system="s")
        k2 = cache_key_for(msgs, system="s")
        assert k1 == k2
        assert len(k1) == 64

    def test_different_system_different_key(self):
        msgs = _make_messages()
        assert cache_key_for(msgs, system="s1") != cache_key_for(msgs, system="s2")

    def test_different_messages_different_key(self):
        assert cache_key_for(_make_messages("a")) != cache_key_for(_make_messages("b"))

    def test_temperature_gt_zero_returns_empty_key(self):
        cfg = LLMConfig(model="m", temperature=0.5, max_tokens=100)
        assert cache_key_for(_make_messages(), config=cfg) == ""

    def test_temperature_zero_returns_real_key(self):
        cfg = LLMConfig(model="m", temperature=0.0, max_tokens=100)
        assert cache_key_for(_make_messages(), config=cfg) != ""


class TestInMemoryCache:
    async def test_set_then_get(self):
        cache = InMemoryCache()
        resp = LLMResponse(content="x", model="m")
        await cache.set("k", resp)
        assert (await cache.get("k")) is resp

    async def test_miss_returns_none(self):
        cache = InMemoryCache()
        assert await cache.get("missing") is None

    async def test_lru_eviction(self):
        cache = InMemoryCache(max_entries=2)
        await cache.set("a", LLMResponse(content="a", model="m"))
        await cache.set("b", LLMResponse(content="b", model="m"))
        await cache.set("c", LLMResponse(content="c", model="m"))
        assert await cache.get("a") is None
        assert (await cache.get("b")).content == "b"
        assert (await cache.get("c")).content == "c"

    async def test_lru_access_promotes(self):
        cache = InMemoryCache(max_entries=2)
        await cache.set("a", LLMResponse(content="a", model="m"))
        await cache.set("b", LLMResponse(content="b", model="m"))
        await cache.get("a")
        await cache.set("c", LLMResponse(content="c", model="m"))
        assert (await cache.get("a")).content == "a"
        assert await cache.get("b") is None


class TestCachingLLMClient:
    async def test_cache_hit_skips_llm(self):
        llm = _CountingLLM()
        client = CachingLLMClient(llm, InMemoryCache())
        cfg = LLMConfig(model="m", temperature=0.0, max_tokens=100)
        msgs = _make_messages()

        r1 = await client.complete(msgs, system="s", config=cfg, on_chunk=noop_chunk)
        r2 = await client.complete(msgs, system="s", config=cfg, on_chunk=noop_chunk)

        assert llm.calls == 1
        assert r1.content == r2.content == "hello"

    async def test_cache_miss_calls_llm(self):
        llm = _CountingLLM()
        client = CachingLLMClient(llm, InMemoryCache())
        cfg = LLMConfig(model="m", temperature=0.0, max_tokens=100)

        await client.complete(_make_messages("a"), config=cfg, on_chunk=noop_chunk)
        await client.complete(_make_messages("b"), config=cfg, on_chunk=noop_chunk)

        assert llm.calls == 2

    async def test_temperature_gt_zero_bypasses_cache(self):
        llm = _CountingLLM()
        client = CachingLLMClient(llm, InMemoryCache())
        cfg = LLMConfig(model="m", temperature=0.7, max_tokens=100)
        msgs = _make_messages()

        await client.complete(msgs, config=cfg, on_chunk=noop_chunk)
        await client.complete(msgs, config=cfg, on_chunk=noop_chunk)

        assert llm.calls == 2

    async def test_cached_response_replays_chunks(self):
        llm = _CountingLLM()
        client = CachingLLMClient(llm, InMemoryCache())
        cfg = LLMConfig(model="m", temperature=0.0, max_tokens=100)
        msgs = _make_messages()

        chunks1: list[str] = []

        async def _cap1(t):
            chunks1.append(t)

        await client.complete(msgs, config=cfg, on_chunk=_cap1)

        chunks2: list[str] = []

        async def _cap2(t):
            chunks2.append(t)

        await client.complete(msgs, config=cfg, on_chunk=_cap2)

        assert chunks1 == chunks2 == ["hello"]

    async def test_cache_store_failure_is_silent(self):
        llm = _CountingLLM()

        class _BrokenStore:
            async def get(self, key):
                return None

            async def set(self, key, resp):
                raise RuntimeError("disk full")

        client = CachingLLMClient(llm, _BrokenStore())
        cfg = LLMConfig(model="m", temperature=0.0, max_tokens=100)
        r = await client.complete(_make_messages(), config=cfg, on_chunk=noop_chunk)
        assert r.content == "hello"

    async def test_no_cache_when_config_is_none(self):
        llm = _CountingLLM()
        client = CachingLLMClient(llm, InMemoryCache())
        await client.complete(_make_messages(), on_chunk=noop_chunk)
        await client.complete(_make_messages(), on_chunk=noop_chunk)
        assert llm.calls == 1

    async def test_cache_hit_sets_from_cache_flag(self):
        llm = _CountingLLM()
        client = CachingLLMClient(llm, InMemoryCache())
        cfg = LLMConfig(model="m", temperature=0.0, max_tokens=100)
        msgs = _make_messages()

        r1 = await client.complete(msgs, config=cfg, on_chunk=noop_chunk)
        r2 = await client.complete(msgs, config=cfg, on_chunk=noop_chunk)

        assert r1.from_cache is False
        assert r2.from_cache is True

    async def test_cache_hit_does_not_mutate_stored_entry(self):
        llm = _CountingLLM()
        client = CachingLLMClient(llm, InMemoryCache())
        cfg = LLMConfig(model="m", temperature=0.0, max_tokens=100)
        msgs = _make_messages()

        await client.complete(msgs, config=cfg, on_chunk=noop_chunk)
        r2 = await client.complete(msgs, config=cfg, on_chunk=noop_chunk)
        r3 = await client.complete(msgs, config=cfg, on_chunk=noop_chunk)

        assert r2.from_cache is True
        assert r3.from_cache is True
        assert llm.calls == 1
