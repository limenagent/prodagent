"""Spend accounting for sub-agent spawns — accumulate, fold, and budget-check."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from prodagent.core.budget import check_budget

if TYPE_CHECKING:
    from prodagent.core.budget import HardBudget
    from prodagent.core.state.run import AgentRun
    from prodagent.core.types import ToolCall
    from prodagent.runtime.coordination.spawn import ChildResult

logger = logging.getLogger(__name__)


def check_spawn_budget(
    run: AgentRun,
    budget: HardBudget | None,
    accumulators: list[SpawnAccumulator],
) -> None:
    """Check budget, folding live sub-agent spend into the parent run totals."""
    if budget is None:
        return
    check_budget(
        run,
        budget,
        extra_turns=sum(a.turns for a in accumulators),
        extra_tokens=sum(a.input_tokens + a.output_tokens for a in accumulators),
        extra_cost_usd=sum(a.cost_usd for a in accumulators),
    )


def fold_spawn_fields(target: Any, source: Any) -> None:
    """Add source's flat spawn-accounting fields onto target, in place."""
    target.cost_usd += source.cost_usd
    target.input_tokens += source.input_tokens
    target.output_tokens += source.output_tokens
    if source.tool_history:
        target.tool_history.extend(source.tool_history)


def fold_spawn_accounting(run: Any, accumulator: SpawnAccumulator | None) -> None:
    """Fold an accumulator's totals onto a run — no-op if nothing was spawned."""
    if accumulator is None or accumulator.spawn_count == 0:
        return
    m = run.metrics
    m.cost_usd += accumulator.cost_usd
    m.input_tokens += accumulator.input_tokens
    m.output_tokens += accumulator.output_tokens
    m.turn_count += accumulator.turns
    if accumulator.tool_history:
        run.tool_history.extend(accumulator.tool_history)
    logger.debug(
        "[spawn] folded %d sub-agent spawns: +$%.4f, +%d turns, +%d tools",
        accumulator.spawn_count,
        accumulator.cost_usd,
        accumulator.turns,
        len(accumulator.tool_history),
    )


@dataclass
class SpawnAccumulator:
    """Shared sink for sub-agent accounting so parent runs can reconcile cost."""

    cost_usd: float = 0.0
    turns: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    spawn_count: int = 0
    tool_history: list[ToolCall] = field(default_factory=list)

    def add(self, result: ChildResult) -> None:
        fold_spawn_fields(self, result)
        self.turns += result.turns
        self.spawn_count += 1
