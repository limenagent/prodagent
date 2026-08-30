"""Conformance tests for ``CacheStore`` implementations."""

from __future__ import annotations

from collections.abc import Callable
from typing import TypeAlias

from prodagent.kernel.types import LLMResponse
from prodagent.ports.observability import CacheStore

Factory: TypeAlias = Callable[[], CacheStore]


def _resp(content: str) -> LLMResponse:
    return LLMResponse(content=content)


async def run_cache_conformance(make_store: Factory) -> None:
    store = make_store()

    assert await store.get("missing") is None

    await store.set("k1", _resp("v1"))
    hit = await store.get("k1")
    assert hit is not None
    assert hit.content == "v1"

    await store.set("k1", _resp("v2"))
    again = await store.get("k1")
    assert again is not None
    assert again.content == "v2", "set must overwrite silently"


async def run_cache_key_isolation_conformance(make_store: Factory) -> None:
    store = make_store()
    await store.set("a", _resp("alpha"))
    await store.set("b", _resp("beta"))
    a = await store.get("a")
    b = await store.get("b")
    assert a is not None and a.content == "alpha"
    assert b is not None and b.content == "beta"
