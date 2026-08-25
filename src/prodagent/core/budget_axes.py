"""Shared axis-precedence check behind :func:`check_budget` and :class:`BudgetLedger`.

Both compare the same four axes in the same order — turns, then seconds, then
tokens, then cost — but build their own exceptions (different context kwargs,
different messages). This function owns only the ordering and the crossed
values; the caller decides what to raise.
"""

from __future__ import annotations

__all__ = ["evaluate_axes"]


def evaluate_axes(
    *,
    turns: int,
    elapsed: float,
    tokens: int,
    cost_usd: float,
    max_turns: int,
    max_seconds: float,
    max_tokens: int,
    max_cost_usd: float,
) -> tuple[str, float, float] | None:
    """First axis at/over its ceiling, in turns → seconds → tokens → cost
    precedence — or ``None`` if all four are under cap."""
    if turns >= max_turns:
        return "turns", turns, max_turns
    if elapsed >= max_seconds:
        return "seconds", elapsed, max_seconds
    if tokens >= max_tokens:
        return "tokens", tokens, max_tokens
    if cost_usd >= max_cost_usd:
        return "cost_usd", cost_usd, max_cost_usd
    return None
