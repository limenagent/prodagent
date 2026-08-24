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
        if self._store is None:
            from prodagent.backends.factory import resolve_cache
            from prodagent.core.config import FrameworkConfig

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
        key = cache_key_for(messages, system=system, tools=tools, config=config)
        store = self._resolve_store() if key else None
        if key and store is not None:
            cached = await store.get(key)
            if cached is not None:
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
                logger.warning("CachingLLMClient: store.set failed: %s", exc)
        return response

    def unwrap(self) -> LLMClient:
        return self._inner


def _copy_with_cache_flag(resp: LLMResponse) -> LLMResponse:
    """Shallow-copy a cached response, flagged so billing skips it."""
    clone = copy.copy(resp)
    clone.from_cache = True
    return clone
