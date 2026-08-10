"""RunLoop — drives a single agent hop, then chains peers if the run hands off."""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING

from prodagent.core.events import RunCompletedEvent, RunFailedEvent, RunSuspendedEvent
from prodagent.core.exceptions import BudgetExceeded, PromptInjectionDetected
from prodagent.core.state.run import AgentRun, child_run_id, is_child_subordinate, make_failed_run
from prodagent.core.types import ExecutionMode, MessageList, RunState
from prodagent.hooks import fire as _fire
from prodagent.hooks import save_and_fire_checkpoint
from prodagent.hooks.checkpoint import CheckPoint
from prodagent.hooks.events import HookEvent
from prodagent.runtime.coordination.accounting import SpawnAccumulator, fold_spawn_accounting
from prodagent.runtime.coordination.budget_ledger import BudgetLedger
from prodagent.runtime.coordination.handoff import HandoffPacket
from prodagent.runtime.factory import LeafExecutorFactory
from prodagent.runtime.run_context import RunContext

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator

    from pydantic import BaseModel

    from prodagent.core.events import AgentEvent
    from prodagent.hooks.registry import HookRegistry
    from prodagent.runtime.agent import Agent

logger = logging.getLogger(__name__)


class RunLoop:
    """Drives an agent run across peer hand-offs, one hop at a time.

    A "hop" is one agent's turn: build its executor via ``LeafExecutorFactory``,
    run it to completion, then check whether it produced a peer hand-off. If so,
    loop again with the peer as the new root agent; otherwise the run is done.
    Not to be confused with :class:`~prodagent.runtime.reactive.ReactiveLoop`,
    which drives the think/act steps *inside* a single hop — ``RunLoop`` never
    talks to an LLM directly, it only orchestrates which agent gets the next hop.
    """

    def __init__(
        self,
        root_agent: Agent,
        initial_ctx: RunContext,
        root_run_id: str,
        output_schema: type[BaseModel] | None,
        *,
        forced_mode: ExecutionMode | None = None,
        initial_messages: MessageList | None = None,
    ) -> None:
        self._root_agent = root_agent
        self._ctx = initial_ctx
        self._root_run_id = root_run_id
        self._output_schema = output_schema
        self._factory = LeafExecutorFactory(
            forced_mode=forced_mode, initial_messages=initial_messages
        )
        # Cross-hop ledger for the peer chain (peers=): without this, each hop's
        # own HardBudget only bounds *that* hop — an N-hop chain could legally
        # spend N times the configured budget, since max_peer_chain only caps hop
        # count, not cumulative spend. One ledger, built from the root agent's own
        # budget, threaded across every hop of this chain.
        self._peer_budget: BudgetLedger | None = (
            BudgetLedger(max=root_agent.budget_config) if root_agent.budget_config else None
        )

    async def run(self) -> AsyncGenerator[AgentEvent, None]:
        overall_final_run: AgentRun | None = None
        settle_hooks: HookRegistry | None = None

        while True:
            ctx = self._ctx
            final_run: AgentRun | None = None
            next_ctx: RunContext | None = None
            try:
                async with ctx:
                    hooks, executor, spawn_acc = await self._factory.prepare(ctx)
                    try:
                        async for event in executor.stream(
                            ctx.task, run_id=ctx.run_id, parent_run_id=ctx.parent_run_id
                        ):
                            if isinstance(
                                event, (RunCompletedEvent, RunFailedEvent, RunSuspendedEvent)
                            ):
                                final_run = event.run
                            yield event
                    except asyncio.CancelledError:
                        if final_run is None:
                            final_run = make_failed_run(
                                ctx.run_id,
                                ctx.task,
                                last_error="run cancelled before terminal event",
                            )
                        overall_final_run = final_run
                        raise
                    finally:
                        if final_run is not None and ctx.run_id != self._root_run_id:
                            final_run.is_peer_continuation = True
                        await self._finalize_run(final_run, ctx, hooks, spawn_acc)

                    if final_run is not None:
                        overall_final_run = final_run

                    next_ctx = await self._next_context(final_run, ctx)
                    if next_ctx is None and overall_final_run is not None:
                        if settle_hooks is None:
                            settle_hooks = self._root_agent.hooks
                            if settle_hooks is None:
                                settle_hooks = self._root_agent.attach_default_hooks()
                        await self._settle(overall_final_run, ctx, settle_hooks)
            except asyncio.CancelledError:
                if overall_final_run is None and final_run is not None:
                    overall_final_run = final_run
                raise

            if next_ctx is None:
                break
            self._ctx = next_ctx
            logger.info(
                "[orchestrator] handoff #%d: → %s (run_id=%s)",
                next_ctx.depth,
                next_ctx.agent.name,
                next_ctx.run_id,
            )

    async def _next_context(
        self,
        run: AgentRun | None,
        ctx: RunContext,
    ) -> RunContext | None:
        if run is None or run.pending_handoff is None:
            return None
        handoff = run.pending_handoff
        fw = self._ctx.agent.framework_config
        if self._ctx.depth >= fw.orchestration.max_peer_chain:
            return None

        peer_name = handoff.peer_name
        peer_spec = self._ctx.agent.peer_named(peer_name)
        if peer_spec is None:
            logger.error(
                "[orchestrator] peer %r not found on agent %r — chain stops",
                peer_name,
                self._ctx.agent.name,
            )
            return None

        if self._peer_budget is not None:
            await self._peer_budget.commit(
                member=self._ctx.agent.name,
                turns=run.turn_count,
                tokens=run.input_tokens + run.output_tokens,
                cost_usd=run.cost_usd,
            )
            try:
                await self._peer_budget.check(member=peer_name)
            except BudgetExceeded as exc:
                logger.warning(
                    "[orchestrator] peer chain budget exhausted before handoff %s → %s: %s",
                    self._ctx.agent.name,
                    peer_name,
                    exc,
                )
                return None

        prior_output = run.final_output or ""
        packet = HandoffPacket(
            task_description=handoff.task,
            constraints=list(self._ctx.agent.constraints),
            available_tools=[t.name for t in peer_spec.inline_tools],
            input_refs=handoff.input_refs or {},
            prior_output=prior_output,
        )
        peer_run_id = child_run_id(self._root_run_id, peer_name)
        handoff.peer_run_id = peer_run_id  # persist on the run before save below
        parent_hooks = self._ctx.agent.hooks
        if parent_hooks is not None:
            await parent_hooks.fire(
                HookEvent.PEER_HANDOFF,
                from_agent=self._ctx.agent.name,
                to_agent=peer_name,
                task=handoff.task[:120] if handoff.task else "",
                depth=self._ctx.depth + 1,
                parent_run_id=self._ctx.run_id,
                child_run_id=peer_run_id,
            )

        if ctx.checkpoint is not None:
            await ctx.checkpoint.save(run, expected_version=run.checkpoint_version)

        return RunContext(
            agent=peer_spec.fork_as_peer(
                self._ctx.agent,
                self._ctx.run_id,
                checkpoint=ctx.checkpoint,
                event_log=ctx.event_log,
            ),
            task=packet.to_task_prompt(),
            run_id=peer_run_id,
            depth=self._ctx.depth + 1,
            parent_run_id=self._ctx.run_id,
        )

    async def _settle(
        self,
        run: AgentRun,
        ctx: RunContext,
        hooks: HookRegistry | None,
    ) -> None:
        if run.state is RunState.SUSPENDED:
            await self._save_checkpoint(run, ctx, hooks)
            return

        if run.state is not RunState.FAILED:
            run.state = RunState.COMPLETED
            if self._output_schema is not None and run.final_output:
                try:
                    from prodagent.llm.structured_output import parse_json_as

                    run.structured_output = parse_json_as(run.final_output, self._output_schema)
                except Exception as exc:
                    run.state = RunState.FAILED
                    run.last_error = f"structured output validation failed: {exc}"
                    logger.warning(
                        "RunLoop[%s] structured output parse failed: %s",
                        run.run_id,
                        exc,
                    )

        if run.state is RunState.FAILED:
            if run.checkpoint_version > 0:
                await self._save_checkpoint(run, ctx, hooks)
        else:
            await self._save_checkpoint(run, ctx, hooks)

        if hooks is None:
            return

        if run.state is RunState.FAILED:
            await _fire(
                hooks,
                HookEvent.RUN_FAILED,
                run_id=run.run_id,
                turns=run.turn_count,
                total_tokens=run.total_tokens,
                cost_usd=run.cost_usd,
                elapsed_s=run.elapsed_seconds(),
                state=run.state.value,
                error=run.last_error or "",
            )
            return

        try:
            await hooks.check_blocking(
                CheckPoint.RUN_COMPLETE,
                run_id=run.run_id,
                final_output=run.final_output or "",
                turns=run.turn_count,
                cost_usd=run.cost_usd,
                state=run.state.value,
            )
        except PromptInjectionDetected:
            run.state = RunState.FAILED
            await self._save_checkpoint(run, ctx, hooks)
            raise

        await _fire(
            hooks,
            HookEvent.RUN_COMPLETE,
            run_id=run.run_id,
            turns=run.turn_count,
            total_tokens=run.total_tokens,
            cost_usd=run.cost_usd,
            elapsed_s=run.elapsed_seconds(),
            state=run.state.value,
        )

    async def _save_checkpoint(
        self,
        run: AgentRun,
        ctx: RunContext,
        hooks: HookRegistry | None = None,
    ) -> None:
        if ctx.checkpoint is not None:
            await save_and_fire_checkpoint(ctx.checkpoint, run, hooks)

    async def _finalize_run(
        self,
        run: AgentRun | None,
        ctx: RunContext,
        hooks: HookRegistry | None,
        spawn_acc: SpawnAccumulator | None,
    ) -> None:
        if run is None:
            run = make_failed_run(ctx.run_id, ctx.task)

        fold_spawn_accounting(run, spawn_acc)

        if not hooks:
            return

        await hooks.fire(
            HookEvent.SESSION_END,
            run=run,
            run_id=run.run_id,
            state=run.state.value,
            turns=run.turn_count,
            cost_usd=run.cost_usd,
            elapsed_s=run.elapsed_seconds(),
            final_output=run.final_output or "",
            messages=list(run.messages),
            depth=ctx.depth,
        )

        for handler in hooks.event_handlers(HookEvent.SESSION_END):
            flush = getattr(handler, "flush", None)
            if flush is None:
                continue
            if is_child_subordinate(run):
                continue
            try:
                await flush()
            except Exception as exc:  # noqa: BLE001 — best-effort drain
                logger.warning(
                    "RunLoop._finalize_run: background flush failed for %r: %s",
                    handler,
                    exc,
                )
