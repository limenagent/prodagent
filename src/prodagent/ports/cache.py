"""CacheStore port — idempotent response cache for LLM complete calls."""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol, runtime_checkable

if TYPE_CHECKING:
    from prodagent.core.types import LLMResponse


@runtime_checkable
class CacheStore(Protocol):
    """Idempotent response cache for LLM complete calls."""

    async def get(self, key: str) -> LLMResponse | None: ...

    async def set(self, key: str, response: LLMResponse) -> None:
        """Overwrites silently on conflict."""
        ...
