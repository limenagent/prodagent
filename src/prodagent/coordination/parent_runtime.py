"""Parent-side runtime context threaded into forked (child/peer) agents."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from prodagent.coordination.accounting import SpawnAccumulator

if TYPE_CHECKING:
    from prodagent.coordination.run_loop import RunContext
    from prodagent.core.budget import HardBudget
    from prodagent.core.config import FrameworkConfig
    from prodagent.hooks.registry import HookRegistry
    from prodagent.llm import LLMClient
    from prodagent.ports import CheckpointStore, EventLog
    from prodagent.runtime.agent import Agent


def describe_agent(a: Agent) -> str:
    """Tool-schema description: prefer ``description``, fall back to truncated system prompt."""
    if a.config.description:
        return a.config.description
    if a.config.system_prompt:
        prompt = a.config.system_prompt[:80]
        return prompt + "..." if len(a.config.system_prompt) > 80 else prompt
    return ""


@dataclass
class ParentRuntime:
    """Parent execution context threaded into every forked Agent.

    Built from :class:`~prodagent.coordination.run_loop.RunContext` when a hop
    spawns (``agents=``) or hands off (``peers=``). Deliberately overlaps ~4
    fields with ``RunContext`` — the subset a forked agent needs (budget, llm,
    checkpoint, event log, depth), not per-hop state (task, run_id, agent
    spec) that's the parent's own business.
    """

    constraints: list[str] = field(default_factory=list)
    budget: HardBudget | None = None
    accumulator: SpawnAccumulator = field(default_factory=SpawnAccumulator)
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
            parent_run_id=ctx.run_id,
            checkpoint=ctx.checkpoint,
            event_log=ctx.event_log,
            peer_specs=list(peer_specs or []),
            depth=ctx.depth,
            llm=ctx.llm,
            hooks=agent.hooks,
            framework_config=agent.framework_config,
        )
