from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

# Re-exported at runtime (prodagent.llm.LLMClient / llm.factory); the `as`
# idiom makes the re-export explicit for mypy's strict no-implicit-reexport.
from prodagent.ports.llm import LLMClient as LLMClient  # noqa: TC001

if TYPE_CHECKING:
    from prodagent.core.types import LLMResponse, MessageList

ChunkCallback = Callable[[str], Awaitable[None]]


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


@dataclass
class LLMConfig:
    model: str = ""
    temperature: float = 0.0
    max_tokens: int = 8_192
    timeout_seconds: float = 60.0
    enable_prompt_caching: bool = True
    cost_per_million_input: float = 0.0
    cost_per_million_output: float = 0.0
    cache_read_discount: float = 0.1

    def __post_init__(self) -> None:
        if not self.model:
            from prodagent.llm.providers import detect_default_model

            self.model = detect_default_model()

    def cost_for_response(self, response: LLMResponse) -> float:
        from prodagent.resilience.cost.pricing import PricingTable, token_cost_usd

        pricing = PricingTable(
            input_rate_per_million=self.cost_per_million_input,
            output_rate_per_million=self.cost_per_million_output,
            cache_read_discount=self.cache_read_discount,
        )
        return token_cost_usd(response, pricing)


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
