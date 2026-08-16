from __future__ import annotations

import pytest

from prodagent.core.types import LLMResponse
from prodagent.resilience.cost.pricing import PricingTable, token_cost_usd

M = 1_000_000


def _resp(
    input_tokens: int = 0,
    output_tokens: int = 0,
    cache_read_tokens: int = 0,
    cache_write_tokens: int = 0,
) -> LLMResponse:
    return LLMResponse(
        content="",
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cache_read_tokens=cache_read_tokens,
        cache_write_tokens=cache_write_tokens,
    )


def test_uncached_tokens_billed_at_full_rate() -> None:
    pricing = PricingTable(input_rate_per_million=3.0, output_rate_per_million=15.0)
    cost = token_cost_usd(_resp(input_tokens=M, output_tokens=M), pricing)
    assert cost == pytest.approx(3.0 + 15.0)


def test_cache_read_billed_at_discount() -> None:
    pricing = PricingTable(input_rate_per_million=3.0, output_rate_per_million=15.0)
    cost = token_cost_usd(_resp(input_tokens=M, cache_read_tokens=M), pricing)
    assert cost == pytest.approx(3.0 * 0.1)


def test_cache_write_billed_at_premium() -> None:
    pricing = PricingTable(input_rate_per_million=3.0, output_rate_per_million=15.0)
    cost = token_cost_usd(_resp(input_tokens=M, cache_write_tokens=M), pricing)
    assert cost == pytest.approx(3.0 * 1.25)


def test_mixed_usage_splits_across_all_three_rates() -> None:
    pricing = PricingTable(input_rate_per_million=3.0, output_rate_per_million=15.0)
    # input_tokens is the inclusive convention: uncached + cache_read + cache_write
    cost = token_cost_usd(
        _resp(
            input_tokens=600_000,
            output_tokens=100_000,
            cache_read_tokens=400_000,
            cache_write_tokens=100_000,
        ),
        pricing,
    )
    expected = (
        100_000 / M * 3.0  # uncached input
        + 100_000 / M * 15.0  # output
        + 400_000 / M * 3.0 * 0.1  # cache read
        + 100_000 / M * 3.0 * 1.25  # cache write
    )
    assert cost == pytest.approx(expected)


def test_negative_billed_input_clamps_to_zero() -> None:
    pricing = PricingTable(input_rate_per_million=3.0, output_rate_per_million=15.0)
    cost = token_cost_usd(_resp(input_tokens=10, cache_read_tokens=500), pricing)
    assert cost == pytest.approx(500 / M * 3.0 * 0.1)


def test_openai_discount_override() -> None:
    pricing = PricingTable(
        input_rate_per_million=2.5,
        output_rate_per_million=10.0,
        cache_read_discount=0.5,
    )
    cost = token_cost_usd(_resp(input_tokens=M, cache_read_tokens=M), pricing)
    assert cost == pytest.approx(2.5 * 0.5)
