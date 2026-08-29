"""LLMClient port — structural interface every provider adapter satisfies.

Single home of :class:`LLMConfig` (``prodagent.llm`` re-exports both):
config is part of the port's contract, so it lives with the protocol, not
with any provider implementation.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from prodagent.kernel.types import LLMResponse, MessageList


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
    ) -> LLMResponse:
        """One model call → one canonical response. ``on_chunk`` streams
        text tokens as they arrive (real-time output / reasoning traces)
        without changing the return shape — adapters choose whether to
        actually stream. Config may be ``None``; the adapter's default
        then applies."""
        ...


@dataclass(frozen=True)
class PricingTable:
    """Per-provider token rates (USD per million tokens)."""

    input_rate_per_million: float
    output_rate_per_million: float
    # Fraction of input rate charged for cache-read tokens (Anthropic 0.1, OpenAI 0.5).
    cache_read_discount: float = 0.1
    # Multiplier on input rate charged for cache-write tokens (Anthropic 1.25).
    cache_write_premium: float = 1.25


def token_cost_usd(response: LLMResponse, pricing: PricingTable) -> float:
    """Cost of one response under a pricing table — the single formula every
    budget and ledger settles with. Four token classes, four price lines:
    plain input and output at list rate, cache-read at a discount, cache-
    write at a premium (providers bundle cache tokens into input_tokens,
    so the plain-input base excludes them)."""
    cache_read = response.cache_read_tokens or 0
    cache_write = response.cache_write_tokens or 0
    # Providers count cache tokens inside input_tokens; they carry their own
    # (discounted / premium) price lines, so the plain-input base excludes them.
    input_billed = max(0, response.input_tokens - cache_read - cache_write)
    cost = (
        # four token classes, four price lines — never a blended average
        input_billed / 1_000_000 * pricing.input_rate_per_million
        + response.output_tokens / 1_000_000 * pricing.output_rate_per_million
        + cache_read / 1_000_000 * (pricing.input_rate_per_million * pricing.cache_read_discount)
        + cache_write / 1_000_000 * (pricing.input_rate_per_million * pricing.cache_write_premium)
    )
    # clamp at zero: refunds (negative cache adjustments) are not a thing here
    return max(0.0, cost)


@dataclass
class LLMConfig:
    """Per-call request settings — and the rate card that keeps the budget's
    cost axis honest (defaults auto-fill from the provider catalog below)."""

    model: str = ""
    temperature: float = 0.0
    max_tokens: int = 8_192
    timeout_seconds: float = 60.0
    enable_prompt_caching: bool = True
    thinking_budget_tokens: int = 0
    cost_per_million_input: float = 0.0
    cost_per_million_output: float = 0.0
    cache_read_discount: float = 0.1
    cache_write_premium: float = 1.25
    cache_boundary_index: int | None = None

    def __post_init__(self) -> None:
        # Convenience default-filling reaches the provider catalog below. The
        # alternative — filling at adapter construction — silently zeroes the
        # cost axis for every bare LLMConfig() passed to complete().
        if not self.model:
            from prodagent.llm.providers import detect_default_model

            self.model = detect_default_model()
        if self.cost_per_million_input == 0.0 and self.cost_per_million_output == 0.0:
            # Rates left unset — fill from the convenience catalog so the cost
            # axis of HardBudget is live by default. Explicit rates always win;
            # unknown models price at zero (FakeLLM included).
            from prodagent.llm.pricing import pricing_for_model

            table = pricing_for_model(self.model)
            if table is not None:
                self.cost_per_million_input = table.input_rate_per_million
                self.cost_per_million_output = table.output_rate_per_million

    def cost_for_response(self, response: LLMResponse) -> float:
        # Pure contract math — no provider package involved.
        pricing = PricingTable(
            input_rate_per_million=self.cost_per_million_input,
            output_rate_per_million=self.cost_per_million_output,
            cache_read_discount=self.cache_read_discount,
            cache_write_premium=self.cache_write_premium,
        )
        return token_cost_usd(response, pricing)
