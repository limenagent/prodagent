"""LLMClient port — structural interface every provider adapter satisfies."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from prodagent.core.types import LLMResponse, MessageList
    from prodagent.llm.base import LLMConfig


@runtime_checkable
class LLMClient(Protocol):
    """Structural interface every provider adapter satisfies (duck-typed)."""

    async def complete(
        self,
        messages: MessageList,
        *,
        system: str | list[dict[str, Any]] = "",
        tools: list[dict[str, Any]] | None = None,
        config: LLMConfig | None = None,
        on_chunk: Callable[[str], Awaitable[None]] | None = None,
    ) -> LLMResponse: ...
