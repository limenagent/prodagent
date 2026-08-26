"""Parent-side runtime context threaded into forked (child/peer) agents."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from prodagent.core.config import FrameworkConfig
    from prodagent.kernel.budget import BudgetLedger, HardBudget
    from prodagent.kernel.bus import HookRegistry
    from prodagent.kernel.state import AgentRun
    from prodagent.kernel.types import ToolCall
    from prodagent.llm import LLMClient
    from prodagent.ports import CheckpointStore, EventLog
    from prodagent.runtime.agent import Agent
    from prodagent.runtime.runner import RunContext

logger = logging.getLogger(__name__)


def describe_agent(a: Agent) -> str:
    """Tool-schema description: prefer ``description``, fall back to truncated system prompt."""
    if a.config.description:
        return a.config.description
    if a.config.system_prompt:
        prompt = a.config.system_prompt[:80]
        return prompt + "..." if len(a.config.system_prompt) > 80 else prompt
    return ""


def fold_spawn_fields(target: Any, source: Any) -> None:
    """Add source's flat spawn-accounting fields onto target, in place."""
    target.cost_usd += source.cost_usd
    target.input_tokens += source.input_tokens
    target.output_tokens += source.output_tokens
    if source.tool_history:
        target.tool_history.extend(source.tool_history)


def hop_own_share(run: AgentRun, acc: SpawnAccumulator | None) -> tuple[int, int, float]:
    """(turns, tokens, cost_usd) the hop itself spent, children excluded.

    By relay time ``RunLoop._finalize_run`` has already folded the
    accumulator's child totals into ``run.metrics`` (via
    :meth:`SpawnAccumulator.fold_into`), so the run's totals are
    *hop + children*. Children committed their own spend to the ledger live
    when they finished; committing the post-fold numbers at handoff would
    count them twice. This subtraction — the only place that knows the fold
    happened — recovers the hop's own share for the settle-at-boundary commit.
    """
    child_turns = acc.turns if acc is not None else 0
    child_tokens = acc.input_tokens + acc.output_tokens if acc is not None else 0
    child_cost = acc.cost_usd if acc is not None else 0.0
    return (
        max(0, run.turn_count - child_turns),
        max(0, run.input_tokens + run.output_tokens - child_tokens),
        max(0.0, run.cost_usd - child_cost),
    )


@dataclass
class SpawnAccumulator:
    """Shared sink for sub-agent spend so parent runs can reconcile cost.

    The enforcement view is the shared ``BudgetLedger`` (kernel/budget.py);
    this accumulator is the metrics/transcript fold sink — child spend that
    must land on the parent's persisted ``AgentRun.metrics`` at hop end.
    """

    cost_usd: float = 0.0
    turns: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    spawn_count: int = 0
    tool_history: list[ToolCall] = field(default_factory=list)

    def add(self, result: Any) -> None:
        fold_spawn_fields(self, result)
        self.turns += result.turns
        self.spawn_count += 1

    def fold_into(self, run: AgentRun) -> None:
        """Fold accumulator totals onto a run's persisted metrics, in place.

        The single home for the accumulator→metrics arithmetic (the other
        direction — child result→accumulator — is :func:`fold_spawn_fields`);
        ``RunLoop._finalize_run`` calls this at hop end so child spend lands
        on the parent's persisted ``AgentRun.metrics``. No-op when nothing
        was spawned.
        """
        if self.spawn_count == 0:
            return
        m = run.metrics
        m.cost_usd += self.cost_usd
        m.input_tokens += self.input_tokens
        m.output_tokens += self.output_tokens
        m.turn_count += self.turns
        if self.tool_history:
            run.tool_history.extend(self.tool_history)
        logger.debug(
            "[spawn] folded %d sub-agent spawns: +$%.4f, +%d turns, +%d tools",
            self.spawn_count,
            self.cost_usd,
            self.turns,
            len(self.tool_history),
        )


@dataclass
class ParentRuntime:
    """Parent execution context threaded into every forked Agent.

    Built from :class:`~prodagent.runtime.runner.RunContext` when a hop
    spawns (``agents=``) or hands off (``peers=``). Deliberately overlaps ~4
    fields with ``RunContext`` — the subset a forked agent needs (budget, llm,
    checkpoint, event log, depth), not per-hop state (task, run_id, agent
    spec) that's the parent's own business.
    """

    constraints: list[str] = field(default_factory=list)
    budget: HardBudget | None = None
    accumulator: SpawnAccumulator = field(default_factory=SpawnAccumulator)
    budget_ledger: BudgetLedger | None = None
    parent_run_id: str | None = None
    checkpoint: CheckpointStore | None = None
    event_log: EventLog | None = None
    peer_specs: list[Agent] = field(default_factory=list)
    depth: int = 0
    llm: LLMClient | None = None
    hooks: HookRegistry | None = None
    framework_config: FrameworkConfig | None = None

    @classmethod
    def from_context(
        cls,
        ctx: RunContext,
        *,
        peer_specs: list[Agent] | None = None,
        accumulator: SpawnAccumulator | None = None,
    ) -> ParentRuntime:
        agent = ctx.agent
        return cls(
            constraints=agent.constraints,
            budget=agent.budget_config,
            accumulator=accumulator or SpawnAccumulator(),
            budget_ledger=ctx.budget_ledger,
            parent_run_id=ctx.run_id,
            checkpoint=ctx.checkpoint,
            event_log=ctx.event_log,
            peer_specs=list(peer_specs or []),
            depth=ctx.depth,
            llm=ctx.llm,
            hooks=agent.hooks,
            framework_config=agent.framework_config,
        )
