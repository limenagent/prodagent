"""The convenience pricing catalog — model-name prefix → published list rates.

The cost FORMULA (``PricingTable`` / ``token_cost_usd``) lives on the port
(:mod:`prodagent.ports.llm`); this module is provider knowledge: which
model names map to which rates.
"""

from __future__ import annotations

from prodagent.ports.llm import PricingTable, token_cost_usd  # noqa: F401 — re-export for compat

# Convenience catalog: model-name prefix → published list rates (USD per
# million tokens, snapshot 2026-08). This exists so the cost axis of
# :class:`~prodagent.kernel.budget.HardBudget` works out of the box; it is
# NOT a quote. Vendor prices move, regional/promo pricing differs — explicit
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


__all__ = ["PricingTable", "token_cost_usd", "MODEL_PRICING", "pricing_for_model"]


def pricing_for_model(model: str) -> PricingTable | None:
    """Longest-prefix catalog lookup, case-insensitive. ``None`` when unknown —
    an unknown model prices at zero unless the caller configures rates."""
    needle = model.strip().lower()
    for prefix, table in MODEL_PRICING:
        if needle.startswith(prefix):
            return table
    return None
