"""Token-cost formula and pricing model."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from prodagent.core.types import LLMResponse


@dataclass(frozen=True)
class PricingTable:
    """Per-provider token rates (USD per million tokens)."""

    input_rate_per_million: float
    output_rate_per_million: float
    # Fraction of input rate charged for cache-read tokens (Anthropic 0.1, OpenAI 0.5).
    cache_read_discount: float = 0.1


def token_cost_usd(response: LLMResponse, pricing: PricingTable) -> float:
    cache_read = response.cache_read_tokens or 0
    input_billed = max(0, response.input_tokens - cache_read)
    cost = (
        input_billed / 1_000_000 * pricing.input_rate_per_million
        + response.output_tokens / 1_000_000 * pricing.output_rate_per_million
        + cache_read / 1_000_000 * (pricing.input_rate_per_million * pricing.cache_read_discount)
    )
    return max(0.0, cost)
