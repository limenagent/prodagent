"""AgentConfig — typed configuration container for Agent."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Callable

    from prodagent.base.config import FrameworkConfig
    from prodagent.cognition.context.spill import ToolResultSpillStore
    from prodagent.cognition.memory.manager import MemoryProvider
    from prodagent.coordination.messaging.contract import MessageContract
    from prodagent.hooks.approval.gate import ApprovalProvider
    from prodagent.kernel.budget import HardBudget, SpawnAccumulator
    from prodagent.kernel.bus import Gate, HookEvent, HookRegistry, InjectionPoint
    from prodagent.kernel.graph import Plan
    from prodagent.kernel.registry import UnitRegistry
    from prodagent.kernel.scheduler import PlannerPort
    from prodagent.llm import LLMClient
    from prodagent.mcp.config import MCPServerConfig
    from prodagent.ports import CheckpointStore, EventLog, SessionStore, Tool
    from prodagent.ports.persistence import BlobStore
    from prodagent.runtime.agent import Agent
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
    checkpoint: CheckpointStore | None = None
    event_log: EventLog | None = None
    blob_store: BlobStore | None = None
    """Explicit spill target for oversized boundary facts; when None and an
    event log is configured, the runtime resolves one from the profile."""
    session_store: SessionStore | None = None
    spill_store: ToolResultSpillStore | None = None
    output_contract: MessageContract | None = None
    spawn_accumulator: SpawnAccumulator | None = None
    initial_plan: Plan | None = None
    planner: PlannerPort | None = None
    registry: UnitRegistry | None = None
    """Named units the planner may draft steps against (``"unit": name``)
    and checkpoints resolve ``unit_ref`` through — one roster per agent."""
    """Per-turn drafting front-end: with no preset plan and no planner, a
    chat turn runs the agent itself as the unit; with a planner, each turn
    drafts a fresh graph. Composition decides, not a mode enum."""
    node_fns: dict[str, Callable[..., Any]] | None = None
    """Plain functions fn nodes invoke, by name — populated when a Workflow
    is bound (its declaration), consumed by the composition root's
    UnitContext at execution."""
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
