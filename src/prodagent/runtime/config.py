"""AgentConfig — typed configuration container for Agent."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from prodagent.core.types import ExecutionMode

if TYPE_CHECKING:
    from collections.abc import Iterable

    from prodagent.cognition.context.spill import ToolResultSpillStore
    from prodagent.cognition.memory.manager import MemoryProvider
    from prodagent.core.budget import HardBudget
    from prodagent.core.config import FrameworkConfig
    from prodagent.evaluation.skills.registry import SkillRegistry
    from prodagent.guardrail.approval.gate import ApprovalProvider
    from prodagent.hooks.registry import HookRegistry
    from prodagent.llm.base import LLMClient
    from prodagent.mcp.config import MCPServerConfig
    from prodagent.ports import CheckpointStore, EventLog, SessionStore, Tool
    from prodagent.runtime.agent import Agent
    from prodagent.runtime.coordination.comm import HandoffContract, SpawnAccumulator
    from prodagent.runtime.plan.dag import Plan
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
    context: str = ""
    framework_config: FrameworkConfig | None = None
    hooks: HookRegistry | None = None
    mode: ExecutionMode = ExecutionMode.PLAN_FIRST
    checkpoint: CheckpointStore | None = None
    event_log: EventLog | None = None
    session_store: SessionStore | None = None
    spill_store: ToolResultSpillStore | None = None
    output_contract: HandoffContract | None = None
    spawn_accumulator: SpawnAccumulator | None = None
    initial_plan: Plan | None = None
    max_replans: int = 2
    description: str = ""
    child_agents: list[Agent] = field(default_factory=list)
    peer_agents: list[Agent] = field(default_factory=list)
    mcp_configs: list[MCPServerConfig] = field(default_factory=list)
    approval_gate: ApprovalProvider | None = None
    memory_manager: MemoryProvider | None = None

    injectors: list[tuple[Any, Any]] = field(default_factory=list)
    checkers: list[tuple[Any, Any]] = field(default_factory=list)
    event_handlers: list[tuple[Any, Any]] = field(default_factory=list)
    extensions: list[object] = field(default_factory=list)


def merge_tools_by_name(existing: list[Tool], new: Iterable[Tool]) -> list[Tool]:
    """Append tools from ``new`` whose name isn't already in ``existing``. Returns what was added."""
    names = {t.name for t in existing}
    added: list[Tool] = []
    for tool in new:
        if tool.name not in names:
            existing.append(tool)
            names.add(tool.name)
            added.append(tool)
    return added
