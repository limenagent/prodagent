"""Hard budget — the multi-axis ceiling that forces a loop to terminate."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING

from prodagent.core.exceptions import BudgetExceeded

if TYPE_CHECKING:
    from prodagent.core.state.run import AgentRun

logger = logging.getLogger(__name__)


@dataclass
class HardBudget:
    """Conservative defaults: unattended runs fail fast rather than burning quota."""

    max_turns: int = 20
    max_seconds: float = 120.0
    max_tokens: int = 100_000
    max_cost_usd: float = 1.0


def check_budget(
    run: AgentRun,
    budget: HardBudget,
    *,
    extra_turns: int = 0,
    extra_tokens: int = 0,
    extra_cost_usd: float = 0.0,
) -> None:
    turn_count = run.turn_count + extra_turns
    if turn_count >= budget.max_turns:
        raise BudgetExceeded(
            f"Turn limit reached: {turn_count}/{budget.max_turns}",
            run_id=run.run_id,
            axis="turns",
            value=turn_count,
            limit=budget.max_turns,
        )

    elapsed = run.elapsed_seconds()
    if elapsed >= budget.max_seconds:
        raise BudgetExceeded(
            f"Time limit reached: {elapsed:.1f}s/{budget.max_seconds}s",
            run_id=run.run_id,
            axis="seconds",
            value=elapsed,
            limit=budget.max_seconds,
        )

    total_tokens = run.input_tokens + run.output_tokens + extra_tokens
    billable_tokens = total_tokens - run.cache_read_tokens
    if billable_tokens >= budget.max_tokens:
        cached = run.cache_read_tokens
        raise BudgetExceeded(
            f"Token limit reached: {billable_tokens}/{budget.max_tokens}"
            + (f" ({cached} cached tokens excluded)" if cached else ""),
            run_id=run.run_id,
            axis="tokens",
            value=billable_tokens,
            limit=budget.max_tokens,
        )

    cost_usd = run.cost_usd + extra_cost_usd
    if cost_usd >= budget.max_cost_usd:
        raise BudgetExceeded(
            f"Cost limit reached: ${cost_usd:.4f}/${budget.max_cost_usd}",
            run_id=run.run_id,
            axis="cost_usd",
            value=cost_usd,
            limit=budget.max_cost_usd,
        )
