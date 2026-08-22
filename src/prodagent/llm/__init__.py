"""LLM adapters — the client contract lives in ports.llm; everything
provider-side (adapters, fake, cache, pricing) resolves lazily."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Any

from prodagent.core.lazy import lazy_package
from prodagent.ports.llm import LLMClient as LLMClient  # noqa: TC001
from prodagent.ports.llm import LLMConfig as LLMConfig  # noqa: TC001

if TYPE_CHECKING:
    from prodagent.core.types import LLMResponse, MessageList

ChunkCallback = Callable[[str], Awaitable[None]]

_SYMBOL_SOURCES: dict[str, str] = {
    "FakeLLMAdapter": "prodagent.llm.fake",
    "RoutingFakeLLM": "prodagent.llm.fake",
    "script": "prodagent.llm.fake",
    "use_fake_llm": "prodagent.llm.providers",
    "create_llm_client": "prodagent.llm.factory",
}

__all__ = sorted(_SYMBOL_SOURCES)

__getattr__, __dir__ = lazy_package(_SYMBOL_SOURCES)


async def noop_chunk(_text: str) -> None:
    """Default on_chunk for callers that don't need per-token callbacks."""


def normalise_content(content: Any, *, join_text_blocks: bool = False) -> Any:
    """Normalise a model's ``content`` field for the adapter wire format."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        result: list[Any] = []
        for item in content:
            if isinstance(item, dict):
                result.append(item)
            else:
                result.append({"type": "text", "text": str(item)})
        if join_text_blocks and all(b.get("type") == "text" for b in result if isinstance(b, dict)):
            return " ".join(b.get("text", "") for b in result if isinstance(b, dict))
        return result
    return content


async def stream_text(
    llm: LLMClient,
    messages: MessageList,
    *,
    system: str | list[dict[str, Any]] = "",
    config: LLMConfig | None = None,
    include_reasoning: bool = False,
) -> tuple[LLMResponse, str]:
    chunks: list[str] = []

    async def _append(chunk: str) -> None:
        chunks.append(chunk)

    response = await llm.complete(
        messages,
        system=system,
        config=config,
        on_chunk=_append,
    )
    text = response.content or (response.reasoning_content if include_reasoning else "") or ""
    if not text:
        text = "".join(chunks)
    return response, text
