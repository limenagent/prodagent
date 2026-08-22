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
    # Multiplier on input rate charged for cache-write tokens (Anthropic 1.25).
    cache_write_premium: float = 1.25


# Convenience catalog: model-name prefix → published list rates (USD per
# million tokens, snapshot 2026-08). This exists so the cost axis of
# :class:`~prodagent.core.budget.HardBudget` works out of the box; it is NOT a
# quote. Vendor prices move, regional/promo pricing differs — explicit
# ``LLMConfig(cost_per_million_input=..., cost_per_million_output=...)``
# always wins over this table, and budgets meant to bind should be set
# against your invoiced rates.
#
# Ordered longest-prefix-first: "gpt-4o-mini" must outrank "gpt-4o".
MODEL_PRICING: tuple[tuple[str, PricingTable], ...] = (
    ("claude-opus", PricingTable(input_rate_per_million=15.0, output_rate_per_million=75.0)),
    ("claude-sonnet", PricingTable(input_rate_per_million=3.0, output_rate_per_million=15.0)),
    ("claude-haiku", PricingTable(input_rate_per_million=0.8, output_rate_per_million=4.0)),
    ("o4-mini", PricingTable(input_rate_per_million=1.1, output_rate_per_million=4.4)),
    ("gpt-4.1", PricingTable(input_rate_per_million=2.0, output_rate_per_million=8.0)),
    ("gpt-4o-mini", PricingTable(input_rate_per_million=0.15, output_rate_per_million=0.6)),
    ("gpt-4o", PricingTable(input_rate_per_million=2.5, output_rate_per_million=10.0)),
    ("glm-5", PricingTable(input_rate_per_million=0.6, output_rate_per_million=2.2)),
    ("glm-4", PricingTable(input_rate_per_million=0.6, output_rate_per_million=2.2)),
    ("deepseek-reasoner", PricingTable(input_rate_per_million=0.55, output_rate_per_million=2.19)),
    ("deepseek-chat", PricingTable(input_rate_per_million=0.27, output_rate_per_million=1.1)),
    ("deepseek", PricingTable(input_rate_per_million=0.27, output_rate_per_million=1.1)),
    ("qwen-max", PricingTable(input_rate_per_million=1.6, output_rate_per_million=6.4)),
    ("qwen-plus", PricingTable(input_rate_per_million=0.4, output_rate_per_million=1.2)),
    ("qwen-turbo", PricingTable(input_rate_per_million=0.05, output_rate_per_million=0.2)),
    ("kimi", PricingTable(input_rate_per_million=0.6, output_rate_per_million=2.5)),
    ("moonshot", PricingTable(input_rate_per_million=0.6, output_rate_per_million=2.5)),
)


def pricing_for_model(model: str) -> PricingTable | None:
    """Longest-prefix catalog lookup, case-insensitive. ``None`` when unknown —
    an unknown model prices at zero unless the caller configures rates."""
    needle = model.strip().lower()
    for prefix, table in MODEL_PRICING:
        if needle.startswith(prefix):
            return table
    return None


def token_cost_usd(response: LLMResponse, pricing: PricingTable) -> float:
    cache_read = response.cache_read_tokens or 0
    cache_write = response.cache_write_tokens or 0
    input_billed = max(0, response.input_tokens - cache_read - cache_write)
    cost = (
        input_billed / 1_000_000 * pricing.input_rate_per_million
        + response.output_tokens / 1_000_000 * pricing.output_rate_per_million
        + cache_read / 1_000_000 * (pricing.input_rate_per_million * pricing.cache_read_discount)
        + cache_write / 1_000_000 * (pricing.input_rate_per_million * pricing.cache_write_premium)
    )
    return max(0.0, cost)
