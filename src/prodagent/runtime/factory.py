"""LeafExecutorFactory — build a LeafExecutor + hooks for one hop of the RunLoop."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from prodagent.core.budget import HardBudget
from prodagent.core.exceptions import PermissionDenied
from prodagent.core.types import ExecutionMode, MessageList
from prodagent.hooks.checkpoint import CheckPoint
from prodagent.hooks.events import HookEvent
from prodagent.mcp.registry import MCPRegistry
from prodagent.runtime.config import merge_tools_by_name
from prodagent.runtime.coordination.peer import assemble_peer_tools
from prodagent.runtime.coordination.spawn import assemble_spawn_tools
from prodagent.runtime.executors.plan_first import PlanExecutor
from prodagent.runtime.executors.reactive import AgentLoop
from prodagent.tooling.builtin.read_tool_result import make_read_tool_result
from prodagent.tooling.dispatcher import ToolDispatcher

if TYPE_CHECKING:
    from prodagent.cognition.context.manager import ContextManager
    from prodagent.core.config import FrameworkConfig
    from prodagent.hooks.registry import HookRegistry
    from prodagent.ports import LeafExecutor
    from prodagent.runtime.agent import Agent
    from prodagent.runtime.coordination.comm import SpawnAccumulator
    from prodagent.runtime.session import RunContext

logger = logging.getLogger(__name__)


class LeafExecutorFactory:
    """Builds the LeafExecutor + hooks registry for one hop."""

    def __init__(
        self,
        *,
        forced_mode: ExecutionMode | None = None,
        initial_messages: MessageList | None = None,
    ) -> None:
        self._forced_mode = forced_mode
        self._initial_messages = initial_messages

    async def prepare(
        self,
        ctx: RunContext,
    ) -> tuple[HookRegistry | None, LeafExecutor, SpawnAccumulator | None]:
        hooks = ctx.agent.attach_default_hooks()
        await self._gate_session_start(hooks, ctx)
        executor, spawn_acc = await self._build_executor(ctx)
        return hooks, executor, spawn_acc

    async def _gate_session_start(self, hooks: HookRegistry | None, ctx: RunContext) -> None:
        if hooks is None:
            return
        result = await hooks.check_blocking(
            CheckPoint.SESSION_START,
            run_id=ctx.run_id,
            task=ctx.task[:120],
            depth=ctx.depth,
        )
        if result.blocked:
            raise PermissionDenied(result.reason or "session blocked at start")
        await hooks.fire(
            HookEvent.SESSION_START,
            run_id=ctx.run_id,
            task=ctx.task[:120],
            depth=ctx.depth,
        )
        await hooks.fire(
            HookEvent.SKILLS_READY,
            count=len(ctx.agent.skills) if ctx.agent.skills else 0,
            names=ctx.agent.skills.names() if ctx.agent.skills else [],
            run_id=ctx.run_id,
        )

    async def _build_executor(
        self,
        ctx: RunContext,
    ) -> tuple[LeafExecutor, SpawnAccumulator | None]:
        agent = ctx.agent
        fw = agent.framework_config

        active_tools, tool_schemas, spawn_acc = await self._resolve_tools(ctx)
        dispatcher, ctx_manager, system, effective_budget = self._build_runtime(
            ctx, fw, active_tools
        )
        executor = self._construct_executor(
            ctx,
            fw,
            dispatcher,
            ctx_manager,
            system,
            effective_budget,
            tool_schemas,
            spawn_acc,
        )
        return executor, spawn_acc

    async def _resolve_tools(
        self,
        ctx: RunContext,
    ) -> tuple[list[Any], list[dict[str, Any]], SpawnAccumulator | None]:
        agent = ctx.agent
        active_tools = await self._resolve_active_tools(agent, ctx)
        tool_schemas: list[dict[str, Any]] = [t.schema for t in active_tools]
        if agent.skills:
            tool_schemas.append(agent.skills.as_tool_schema())
        spawn_acc = assemble_spawn_tools(ctx, active_tools, tool_schemas)
        assemble_peer_tools(ctx, active_tools, tool_schemas, spawn_acc)
        return active_tools, tool_schemas, spawn_acc

    def _build_runtime(
        self,
        ctx: RunContext,
        fw: FrameworkConfig,
        active_tools: list[Any],
    ) -> tuple[ToolDispatcher, ContextManager, str, HardBudget]:
        agent = ctx.agent
        system = agent.build_system_prompt()
        effective_budget = agent.budget_config or HardBudget()
        ctx_manager = agent.build_context_manager(system, fw, ctx)
        dispatcher = ToolDispatcher(
            {t.name: t for t in active_tools},
            lock_registry=agent.lock_registry,
            tool_registry=agent.tool_registry,
            hooks=agent.hooks,
            skills=agent.skills,
            agent_id=agent.name,
            agent_name=agent.name,
        )
        return dispatcher, ctx_manager, system, effective_budget

    def _construct_executor(
        self,
        ctx: RunContext,
        fw: FrameworkConfig,
        dispatcher: ToolDispatcher,
        ctx_manager: ContextManager,
        system: str,
        effective_budget: HardBudget,
        tool_schemas: list[dict[str, Any]],
        spawn_acc: SpawnAccumulator | None,
    ) -> LeafExecutor:
        agent = ctx.agent
        spawn_accumulators = [
            acc for acc in (agent.config.spawn_accumulator, spawn_acc) if acc is not None
        ]
        is_root = ctx.depth == 0
        effective_mode = (
            self._forced_mode if (is_root and self._forced_mode is not None) else agent.mode
        )
        initial_messages = self._initial_messages if is_root else None

        if effective_mode is ExecutionMode.PLAN_FIRST:
            return PlanExecutor(
                ctx.llm,
                dispatcher.dispatch,
                system=system,
                messages=initial_messages or [{"role": "user", "content": ""}],
                hooks=agent.hooks,
                agent_name=agent.name,
                tool_schemas=tool_schemas,
                event_log=ctx.event_log,
                checkpoint_store=ctx.checkpoint,
                framework_config=fw,
                budget=effective_budget,
                spawn_accumulators=spawn_accumulators,
                initial_plan=agent.initial_plan,
                max_replans=agent.max_replans,
                dispatcher=dispatcher,
            )
        return AgentLoop(
            ctx.llm,
            dispatcher,
            system_prompt=system,
            tools_schema=tool_schemas,
            budget=effective_budget,
            context_manager=ctx_manager,
            hooks=agent.hooks,
            loop_config=fw.loop,
            checkpoint_store=ctx.checkpoint,
            spill_store=ctx.spill_store,
            spawn_accumulators=spawn_accumulators,
            initial_messages=initial_messages,
        )

    async def _resolve_active_tools(
        self,
        agent: Agent,
        ctx: RunContext,
    ) -> list[Any]:
        active_tools = await agent.resolve_tools()
        mcp_tools = await self._collect_mcp_tools(agent, ctx)
        merge_tools_by_name(active_tools, mcp_tools)
        if ctx.spill_store is not None:
            merge_tools_by_name(active_tools, [make_read_tool_result(ctx.spill_store)])
        return active_tools

    async def _collect_mcp_tools(
        self,
        agent: Agent,
        ctx: RunContext,
    ) -> list[Any]:
        if not agent.mcp_configs:
            return []
        try:
            registry = await ctx.stack.enter_async_context(MCPRegistry(agent.mcp_configs))
            mcp_tools = await registry.get_tools() or []
        except Exception as exc:
            logger.warning("MCP registry setup failed; continuing without MCP tools: %s", exc)
            return []
        if mcp_tools:
            logger.info("MCP tools attached: %s", ", ".join(t.name for t in mcp_tools))
        return mcp_tools
