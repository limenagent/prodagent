"""AgentConfig — typed configuration container for Agent."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from prodagent.kernel.types import ExecutionMode

if TYPE_CHECKING:
    from collections.abc import Callable

    from prodagent.base.config import FrameworkConfig
    from prodagent.cognition.context.spill import ToolResultSpillStore
    from prodagent.cognition.memory.manager import MemoryProvider
    from prodagent.coordination.messaging.contract import MessageContract
    from prodagent.hooks.approval.gate import ApprovalProvider
    from prodagent.kernel.budget import HardBudget
    from prodagent.kernel.bus import Gate, HookEvent, HookRegistry, InjectionPoint
    from prodagent.llm import LLMClient
    from prodagent.mcp.config import MCPServerConfig
    from prodagent.plan.dag import Plan
    from prodagent.ports import CheckpointStore, EventLog, SessionStore, Tool
    from prodagent.runtime.agent import Agent
    from prodagent.runtime.parent_runtime import SpawnAccumulator
    from prodagent.skills.registry import SkillRegistry
    from prodagent.tooling.registry import ToolRegistry


@dataclass
class AgentConfig:
    name: str

    llm: LLMClient | None = None
    tools: list[Tool] = field(default_factory=list)
    tool_registry: ToolRegistry | None = None
    skills: SkillRegistry | None = None
    budget: HardBudget | None = None
    constraints: list[str] = field(default_factory=list)
    system_prompt: str = ""
    framework: FrameworkConfig | None = None
    hooks: HookRegistry | None = None
    mode: ExecutionMode = ExecutionMode.REACTIVE
    checkpoint: CheckpointStore | None = None
    event_log: EventLog | None = None
    session_store: SessionStore | None = None
    spill_store: ToolResultSpillStore | None = None
    output_contract: MessageContract | None = None
    spawn_accumulator: SpawnAccumulator | None = None
    initial_plan: Plan | None = None
    max_replans: int = 2
    description: str = ""
    agents: list[Agent] = field(default_factory=list)
    peers: list[Agent] = field(default_factory=list)
    mcp: list[MCPServerConfig] = field(default_factory=list)
    approval: ApprovalProvider | None = None
    memory: MemoryProvider | None = None

    injectors: list[tuple[InjectionPoint, Callable[..., Any]]] = field(default_factory=list)
    checkers: list[tuple[Gate, Callable[..., Any]]] = field(default_factory=list)
    event_handlers: list[tuple[HookEvent, Callable[..., Any]]] = field(default_factory=list)
    extensions: list[object] = field(default_factory=list)
