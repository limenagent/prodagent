"""LeafExecutorFactory — build a LeafExecutor + hooks for one hop of the RunLoop.

One public method (``prepare``) and three build steps, top to bottom:
tools → runtime → executor. No pass-through relays.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from prodagent.core.budget import SAFETY_NET_BUDGET
from prodagent.core.exceptions import PermissionDenied
from prodagent.core.types import ExecutionMode, MessageList
from prodagent.hooks.events import HookEvent
from prodagent.hooks.gates import Gate
from prodagent.runtime._tool_merge import merge_tools_by_name
from prodagent.runtime.reactive import ReactiveLoop
from prodagent.tooling.dispatcher import ToolDispatcher

if TYPE_CHECKING:
    from prodagent.runtime.parent_runtime import SpawnAccumulator
    from prodagent.runtime.runner import RunContext
    from prodagent.hooks.registry import HookRegistry
    from prodagent.ports import LeafExecutor
    from prodagent.runtime.agent import Agent

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
        agent = ctx.agent
        fw = agent.framework_config
        hooks = agent.attach_default_hooks()
        await self._gate_session_start(hooks, ctx)

        # 1. tools: inline + registry + MCP + spill reader + spawn/peer wrappers
        active_tools = await agent.resolve_tools()
        mcp_tools = await self._collect_mcp_tools(agent, ctx)
        merge_tools_by_name(active_tools, mcp_tools)
        if ctx.spill_store is not None:
            from prodagent.tooling.builtin.read_tool_result import make_read_tool_result

            merge_tools_by_name(active_tools, [make_read_tool_result(ctx.spill_store)])
        tool_schemas: list[dict[str, Any]] = [t.schema for t in active_tools]
        if agent.skills:
            tool_schemas.append(agent.skills.as_tool_schema())
        # Hop tools arrive via the ctx seam — the factory stays blind to
        # which collaboration capabilities exist.
        spawn_acc = None
        for assembler in ctx.tool_assemblers:
            spawn_acc = assembler(ctx, active_tools, tool_schemas, spawn_acc) or spawn_acc

        # 2. runtime: dispatcher + optional context manager + budget + prompt
        system = agent.build_system_prompt()
        effective_budget = agent.budget_config or SAFETY_NET_BUDGET
        # Compression is opt-in: bare agents send the loop's messages through
        # untouched (ReactiveLoop handles a None context manager).
        ctx_manager = (
            agent.build_context_manager(system, fw, ctx) if fw.context.compression else None
        )
        dispatcher = ToolDispatcher(
            {t.name: t for t in active_tools},  # type: ignore[misc]
            tool_registry=agent.config.tool_registry,
            hooks=agent.hooks,
            skills=agent.skills,
            agent_id=agent.name,
            agent_name=agent.name,
        )

        # 3. executor: PLAN_FIRST (dynamic or preset DAG) vs REACTIVE loop
        # Enforcement rides the chain ledger; only the cross-hop config-level
        # accumulator still reaches the executors as a live-spend view.
        spawn_accumulators = (
            [agent.config.spawn_accumulator] if agent.config.spawn_accumulator else []
        )
        is_root = ctx.depth == 0
        effective_mode = (
            self._forced_mode if (is_root and self._forced_mode is not None) else agent.mode
        )
        initial_messages = self._initial_messages if is_root else None

        if effective_mode is ExecutionMode.PLAN_FIRST:
            from prodagent.plan.executor import PlanExecutor

            executor: LeafExecutor = PlanExecutor(
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
                initial_plan=agent.config.initial_plan,
                budget_ledger=ctx.budget_ledger,
                max_replans=agent.config.max_replans,
                dispatcher=dispatcher,
            )
        else:
            executor = ReactiveLoop(
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
                budget_ledger=ctx.budget_ledger,
            )
        return hooks, executor, spawn_acc

    async def _gate_session_start(self, hooks: HookRegistry | None, ctx: RunContext) -> None:
        if hooks is None:
            return
        result = await hooks.check_blocking(
            Gate.SESSION_START,
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
        if ctx.agent.skills is not None:
            await hooks.fire(
                HookEvent.SKILLS_READY,
                count=len(ctx.agent.skills),
                names=ctx.agent.skills.names(),
                run_id=ctx.run_id,
            )

    async def _collect_mcp_tools(self, agent: Agent, ctx: RunContext) -> list[Any]:
        if not agent.mcp_configs:
            return []
        try:
            from prodagent.mcp.registry import MCPRegistry

            registry = await ctx.stack.enter_async_context(MCPRegistry(agent.mcp_configs))
            mcp_tools = await registry.get_tools() or []
        except Exception as exc:
            logger.warning("MCP registry setup failed; continuing without MCP tools: %s", exc)
            return []
        if mcp_tools:
            logger.info("MCP tools attached: %s", ", ".join(t.name for t in mcp_tools))
        return mcp_tools
