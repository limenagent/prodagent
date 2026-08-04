"""Fork an Agent spec for peer handoff or sub-agent spawn."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Literal

from prodagent.runtime.coordination.comm import SpawnAccumulator

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


def fork_agent(
    source: Agent,
    parent: Agent,
    *,
    runtime: ParentRuntime,
    mode: Literal["peer", "spawn"],
) -> Agent:
    """Build a fresh ``Agent`` from ``source``'s declarative spec, wired with
    ``runtime``'s parent-side fields.
    """
    from prodagent.runtime.agent import Agent

    forked = Agent(
        source.name,
        tools=list(source.inline_tools),
        context=source.system_prompt,
        llm=runtime.llm,
        hooks=runtime.hooks,
        framework_config=runtime.framework_config,
        constraints=list(runtime.constraints),
        budget=runtime.budget,
        lock_registry=runtime.lock_registry,
        mode=source.mode,
        checkpoint=runtime.checkpoint,
        event_log=runtime.event_log,
        spawn_accumulator=runtime.accumulator,
    )
    wiring_source = parent if mode == "peer" else source
    forked.config.extensions = list(wiring_source.config.extensions)
    forked.config.injectors = list(wiring_source.config.injectors)
    forked.config.checkers = list(wiring_source.config.checkers)
    forked.config.event_handlers = list(wiring_source.config.event_handlers)
    forked.config.mcp_configs = list(wiring_source.config.mcp_configs)
    forked._fluent_wired = wiring_source._fluent_wired
    if mode == "peer":
        forked.config.peer_agents = list(source.config.peer_agents)
        forked.config.description = source.config.description
    else:
        if source.config.initial_plan is not None:
            forked.config.initial_plan = source.config.initial_plan
            forked.config.max_replans = source.config.max_replans
        if source.config.description:
            forked.config.description = source.config.description
    return forked


def fork_as_peer(
    source: Agent,
    parent: Agent,
    parent_run_id: str | None,
    *,
    checkpoint: CheckpointStore | None = None,
    event_log: EventLog | None = None,
) -> Agent:
    """Build a peer continuation of ``parent`` from the ``source`` spec.

    ``checkpoint``/``event_log`` override the parent's declarative config
    when the caller already holds resolved values (e.g. from a live
    ``RunContext``), since ``parent.config.checkpoint`` may still be unset.
    """
    return fork_agent(
        source,
        parent,
        runtime=ParentRuntime(
            llm=source.config.llm,
            hooks=parent.hooks,
            framework_config=parent.framework_config,
            constraints=parent.constraints,
            budget=source.budget_config,
            lock_registry=parent.lock_registry,
            checkpoint=checkpoint if checkpoint is not None else parent.config.checkpoint,
            event_log=event_log if event_log is not None else parent.config.event_log,
            accumulator=source.config.spawn_accumulator or SpawnAccumulator(),
        ),
        mode="peer",
    )
