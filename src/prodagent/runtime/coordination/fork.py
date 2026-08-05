"""Parent-side runtime context threaded into forked agents."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from prodagent.runtime.coordination.accounting import SpawnAccumulator

if TYPE_CHECKING:
    from prodagent.core.budget import HardBudget
    from prodagent.core.config import FrameworkConfig
    from prodagent.hooks.registry import HookRegistry
    from prodagent.llm.base import LLMClient
    from prodagent.ports import CheckpointStore, EventLog
    from prodagent.runtime.agent import Agent
    from prodagent.runtime.session import RunContext
    from prodagent.tooling.reliability.locks import LockRegistry


def describe_agent(a: Agent) -> str:
    """One-line description for tool schemas: prefers ``description``, falls back to truncated system prompt."""
    if a.config.description:
        return a.config.description
    if a.system_prompt:
        prompt = a.system_prompt[:80]
        return prompt + "..." if len(a.system_prompt) > 80 else prompt
    return ""


@dataclass
class ParentRuntime:
    """Parent execution context threaded into every forked Agent."""

    constraints: list[str] = field(default_factory=list)
    budget: HardBudget | None = None
    lock_registry: LockRegistry | None = None
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
            lock_registry=agent.lock_registry,
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
