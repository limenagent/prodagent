"""LLM response cache wrapper — decorates an ``LLMClient`` with a ``CacheStore``."""

from __future__ import annotations

import copy
import hashlib
import json
import logging
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

from prodagent.kernel.types import LLMResponse, stable_serialize

if TYPE_CHECKING:
    from prodagent.kernel.types import MessageList
    from prodagent.llm import ChunkCallback, LLMConfig
    from prodagent.ports.llm import LLMClient

logger = logging.getLogger(__name__)

__all__ = ["CachingLLMClient", "CachingLLM", "cache_key_for"]


@runtime_checkable
class CachingLLM(Protocol):
    """Marker for an LLM client that wraps a cache."""

    async def complete(
        self,
        messages: MessageList,
        *,
        system: str | list[dict[str, Any]] = "",
        tools: list[dict[str, Any]] | None = None,
        config: LLMConfig | None = None,
        on_chunk: ChunkCallback | None = None,
    ) -> LLMResponse: ...

    def unwrap(self) -> LLMClient: ...


def cache_key_for(
    messages: MessageList,
    *,
    system: str | list[dict[str, Any]] = "",
    tools: list[dict[str, Any]] | None = None,
    config: LLMConfig | None = None,
) -> str:
    """Stable SHA-256 fingerprint of an LLM request."""
    cfg_part = ""
    if config is not None:
        if getattr(config, "temperature", 0.0) > 0.0:
            return ""  # Non-deterministic — caller should skip caching.
        # Only identity-bearing settings enter the key: two calls that differ
        # merely in timeout must still share a cache entry.
        cfg_part = json.dumps(
            {
                "model": config.model,
                "temperature": config.temperature,
                "max_tokens": config.max_tokens,
            },
            sort_keys=True,
            default=str,
        )

    payload = json.dumps(
        {
            "messages": list(messages),
            "system": system,
            "tools": tools or [],
            "config": cfg_part,
        },
        sort_keys=True,
        default=stable_serialize,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


class CachingLLMClient:
    """Decorate an LLMClient with a response cache.

    Streaming callbacks still fire for the cached content so downstream UIs
    render tokens. ``temperature > 0`` bypasses the cache entirely.
    """

    def __init__(
        self,
        inner: LLMClient,
        store: Any = None,  # CacheStore | None
        *,
        framework_config: Any = None,
    ) -> None:
        self._inner = inner
        self._store = store
        self._framework_config = framework_config

    def _resolve_store(self) -> Any:
        """Late-resolve the store on first use — lets compose wrap the LLM
        before backends exist, without paying factory work when every call
        misses anyway."""
        if self._store is None:
            from prodagent.backends.factory import resolve_cache
            from prodagent.base.config import FrameworkConfig

            fw = self._framework_config or FrameworkConfig.default()
            self._store = resolve_cache(fw)
        return self._store

    async def complete(
        self,
        messages: MessageList,
        *,
        system: str | list[dict[str, Any]] = "",
        tools: list[dict[str, Any]] | None = None,
        config: LLMConfig | None = None,
        on_chunk: ChunkCallback | None = None,
    ) -> LLMResponse:
        """Lookup → miss → inner call → best-effort store. A cache hit still
        fires ``on_chunk`` (so streaming UIs behave) and returns a flagged
        copy the billing path skips — replayed tokens were paid for once."""
        key = cache_key_for(messages, system=system, tools=tools, config=config)
        store = self._resolve_store() if key else None  # "" key = uncachable, no store work
        if key and store is not None:
            cached = await store.get(key)
            if cached is not None:
                # Replay the content through on_chunk so streaming UIs can't
                # tell a cache hit from a live call.
                if on_chunk is not None and cached.content:
                    await on_chunk(cached.content)
                return _copy_with_cache_flag(cached)

        response = await self._inner.complete(
            messages,
            system=system,
            tools=tools,
            config=config,
            on_chunk=on_chunk,
        )

        if key and store is not None:
            try:
                await store.set(key, response)
            except Exception as exc:  # noqa: BLE001 — cache write failure is best-effort
                # A cache that can't write is a slower pass-through, never an
                # error the caller should see.
                logger.warning("CachingLLMClient: store.set failed: %s", exc)
        return response

    def unwrap(self) -> LLMClient:
        """Peel the wrapper — compose uses this to avoid double-wrapping."""
        return self._inner


def _copy_with_cache_flag(resp: LLMResponse) -> LLMResponse:
    """Shallow-copy a cached response, flagged so billing skips it."""
    clone = copy.copy(resp)
    clone.from_cache = True
    return clone
