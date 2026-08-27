"""Parent-side runtime context threaded into forked (child/peer) agents.

The spawn-accounting arithmetic that used to live here (``SpawnAccumulator``,
``fold_spawn_fields``, ``hop_own_share``) moved to
:mod:`prodagent.kernel.budget` — enforcement and fold are one settlement
concept. What remains is the wiring object: the parent execution context a
forked Agent rebinds to when it joins a chain.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from prodagent.kernel.budget import SpawnAccumulator

if TYPE_CHECKING:
    from prodagent.base.config import FrameworkConfig
    from prodagent.kernel.budget import BudgetLedger, HardBudget
    from prodagent.kernel.bus import HookRegistry
    from prodagent.llm import LLMClient
    from prodagent.ports import CheckpointStore, EventLog
    from prodagent.runtime.agent import Agent
    from prodagent.runtime.runner import RunContext

logger = logging.getLogger(__name__)

__all__ = ["ParentRuntime"]


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
