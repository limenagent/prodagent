"""SchedulerFactory — build the Scheduler + hooks for one hop of the RunLoop.

One public method (``prepare``) and three build steps, top to bottom:
tools → runtime → engine. No pass-through relays, no mode branches: the
same Scheduler leaves the factory whether the graph comes from a model, a
hand-written Workflow, or the agent itself as a single unit.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from prodagent.base.config import ContextConfig
from prodagent.base.errors import PermissionDenied
from prodagent.kernel.budget import SAFETY_NET_BUDGET
from prodagent.kernel.bus import Gate, HookEvent
from prodagent.kernel.types import MessageList
from prodagent.runtime.agent_loop import AgentLoop
from prodagent.tooling.dispatcher import ToolDispatcher
from prodagent.tooling.merge import merge_tools_by_name

if TYPE_CHECKING:
    from prodagent.kernel.budget import SpawnAccumulator
    from prodagent.kernel.bus import HookRegistry
    from prodagent.kernel.types import MessageList
    from prodagent.ports import Executor
    from prodagent.runtime.agent import Agent
    from prodagent.runtime.runner import RunContext

logger = logging.getLogger(__name__)


def _subagent_invoker(ctx: Any) -> Any:
    """Delegation nodes' activation port: resolve the child on the parent's
    roster, activate through the hop's RunnerPort on the shared
    ``activate_subagent`` core — the same core the spawn tool uses, so both
    entry points grow isomorphic Run trees."""
    from prodagent.coordination.activation import activate_subagent

    agent = ctx.agent
    runner = ctx.runner

    async def _invoke(child_name: str, task: str, run_id: str = "") -> dict[str, Any]:
        if runner is None:
            raise RuntimeError(
                f"subagent node {child_name!r}: no RunnerPort on this hop — "
                "delegation needs the activation port wired by the RunLoop"
            )
        spec = next((a for a in agent.child_agents if a.name == child_name), None)
        if spec is None:
            from prodagent.base.errors import ErrorReason
            from prodagent.kernel.types import ToolError

            return ToolError.from_reason(
                ErrorReason.TOOL_NOT_AVAILABLE,
                code="subagent_not_found",
                message=f"Unknown sub-agent {child_name!r}. Available: "
                f"{[a.name for a in agent.child_agents]}",
            ).as_dict()
        from dataclasses import asdict

        result = await activate_subagent(
            runner,
            spec,
            task,
            parent_run_id=ctx.run_id,
            depth=ctx.depth + 1,
            budget_ledger=ctx.budget_ledger,
        )
        return asdict(result)

    return _invoke


def _llm_invoker(llm: Any, hooks: Any) -> Any:
    """Fixed-prompt model call for LLM bodies: which client, which default
    config, and the LLM_REQUEST hook are composition decisions — the body
    only declares the prompt. A ``None`` client means no invoker (an llm
    node then fails loudly at execution)."""
    if llm is None:
        return None

    async def _invoke(prompt: str, *, system: str = "", run_id: str = "") -> str:
        from prodagent.hooks import fire as _fire
        from prodagent.kernel.bus import HookEvent
        from prodagent.llm import noop_chunk

        await _fire(
            hooks,
            HookEvent.LLM_REQUEST,
            system=system[:200],
            system_len=len(system),
            messages=[{"role": "user", "content": prompt}],
            msg_count=1,
            phase="workflow",
            run_id=run_id,
        )
        response = await llm.complete(
            [{"role": "user", "content": prompt}],
            system=system,
            config=getattr(llm, "default_config", None),
            on_chunk=noop_chunk,
        )
        return response.content or ""

    return _invoke


class SchedulerFactory:
    """Builds the Scheduler + hooks registry for one hop."""

    def __init__(
        self,
        *,
        single_unit: bool = False,
        initial_messages: MessageList | None = None,
    ) -> None:
        self._single_unit = single_unit
        self._initial_messages = initial_messages

    async def prepare(
        self,
        ctx: RunContext,
    ) -> tuple[HookRegistry | None, Executor, SpawnAccumulator | None]:
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
        # untouched (the AgentLoop handles a None context manager).
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
            event_log=ctx.event_log,
            blob_store=ctx.blob_store,
            blob_threshold_bytes=fw.boundary_blob_threshold_bytes,
        )
        is_root = ctx.depth == 0
        # The chat path runs the agent itself as the unit (one-node graph,
        # checkpoint-resumable); the work path runs a drafted or preset
        # graph. An agent with neither a preset nor a planner IS its unit at
        # every hop — composition decides, not a mode enum.
        single_unit = (is_root and self._single_unit) or (
            agent.config.initial_plan is None and agent.config.planner is None
        )
        initial_messages = self._initial_messages if is_root else None

        # 3. the engine: one Scheduler for every shape. (Lazily imported:
        # importing an Agent stays execution-free — the engine loads when a
        # run starts.)
        from prodagent.kernel.scheduler import Scheduler
        from prodagent.kernel.units import AutonomousUnit
        from prodagent.plan.planner import Planner

        engine = AgentLoop(
            ctx.llm,
            dispatcher,
            system_prompt=system,
            tools_schema=tool_schemas,
            budget=effective_budget,
            context_manager=ctx_manager,
            hooks=agent.hooks,
            loop_config=fw.loop,
            checkpoint_store=ctx.checkpoint,
            event_log=ctx.event_log,
            spill_store=ctx.spill_store,
            budget_ledger=ctx.budget_ledger,
        )
        # Both shapes share this dispatcher, so both get spill truncation —
        # graph tool results must not accumulate raw in the transcript
        # either. The single-unit shape additionally fingerprints tool
        # batches through the dead-loop guard; graph nodes fail into
        # replans instead.
        dispatcher.configure_batch(
            loop_config=fw.loop,
            context_config=(
                ctx_manager.config
                if ctx_manager is not None
                else (ContextConfig() if ctx.spill_store is not None else None)
            ),
            spill_store=ctx.spill_store,
            progress_monitor=engine.progress if single_unit else None,
        )
        executor: Scheduler = Scheduler(
            system=system,
            initial_messages=initial_messages,
            hooks=agent.hooks,
            agent_name=agent.name,
            # Planning is injected: the kernel takes a PlannerPort, the LLM
            # implementation is composed here (one layer up, where it belongs).
            # Parentheses matter: the default planner is an alternative to the
            # injected one, gated by single_unit as a whole.
            planner=(
                agent.config.planner
                or Planner(
                    llm=ctx.llm,
                    config=None,
                    tool_schemas=tool_schemas or [],
                    hooks=agent.hooks,
                    framework_config=fw,
                    registry=agent.config.registry,
                )
                if not single_unit
                else None
            ),
            initial_unit=AutonomousUnit() if single_unit else None,
            # The single-unit shape treats both stores as opt-in (None stays
            # None); graph tracking always tracks DAG state, so it gets the
            # agent's fallbacks.
            event_log=(
                ctx.event_log
                if single_unit
                else ctx.event_log or agent.ensure_plan_event_log_fallback()
            ),
            checkpoint_store=(
                ctx.checkpoint
                if single_unit
                else ctx.checkpoint or agent.ensure_plan_checkpoint_fallback()
            ),
            framework_config=fw,
            budget=effective_budget,
            initial_plan=agent.config.initial_plan,
            budget_ledger=ctx.budget_ledger,
            max_replans=agent.config.max_replans,
            dispatcher=dispatcher,
            engine=engine,
            depth=ctx.depth,
            fns=agent.config.node_fns,
            llm_invoker=_llm_invoker(ctx.llm, hooks),
            subagent=_subagent_invoker(ctx),
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
