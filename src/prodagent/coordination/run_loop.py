"""RunLoop — drives a single agent hop, then chains peers if the run hands off."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import uuid
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from prodagent.coordination.accounting import SpawnAccumulator, fold_spawn_accounting
from prodagent.coordination.budget_ledger import BudgetLedger
from prodagent.coordination.messaging.envelope import Crossing, CrossingKind, Direction
from prodagent.coordination.messaging.idempotency import IdempotentMessageHandler
from prodagent.coordination.messaging.packet import HandoffPacket
from prodagent.coordination.messaging.pipeline import (
    Pipeline,
    admission_pipeline,
    assembly_pipeline,
)
from prodagent.coordination.peer import find_suspended_peer
from prodagent.core.events import RunCompletedEvent, RunFailedEvent, RunSuspendedEvent
from prodagent.core.exceptions import SECURITY_VETO_EXCEPTIONS, BudgetExceeded
from prodagent.core.state.run import AgentRun, child_run_id, is_child_subordinate, make_failed_run
from prodagent.core.types import ExecutionMode, MessageList, RunState
from prodagent.hooks import fire as _fire
from prodagent.hooks import save_and_fire_checkpoint
from prodagent.hooks.events import HookEvent
from prodagent.hooks.gates import Gate
from prodagent.runtime.factory import LeafExecutorFactory

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator

    from pydantic import BaseModel

    from prodagent.cognition.context.spill import ToolResultSpillStore
    from prodagent.core.events import AgentEvent
    from prodagent.hooks.registry import HookRegistry
    from prodagent.ports import CheckpointStore, EventLog
    from prodagent.ports.llm import LLMClient
    from prodagent.runtime.agent import Agent

logger = logging.getLogger(__name__)


# ── RunContext — per-hop input, resolved into runtime dependencies on enter ──


def _resolve_llm(agent: Agent) -> LLMClient:
    from prodagent.backends.factory import resolve_llm

    llm = agent.config.llm or resolve_llm(agent.framework_config)
    if agent.framework_config.profile != "production":
        # Bare kernel: no cache wrapper — a prompt cache is an optimization
        # with observability side effects, not part of the loop.
        return llm
    from prodagent.llm.cache import CachingLLM, CachingLLMClient

    if isinstance(llm, CachingLLM):
        return llm
    return CachingLLMClient(llm, framework_config=agent.framework_config)


@dataclass
class RunContext:
    """Per-hop input: which agent, what task, which run_id, how deep."""

    agent: Agent
    task: str
    run_id: str
    depth: int = 0
    parent_run_id: str | None = None
    llm: LLMClient = field(init=False)
    checkpoint: CheckpointStore | None = field(init=False, default=None)
    event_log: EventLog | None = field(init=False, default=None)
    spill_store: ToolResultSpillStore | None = field(init=False, default=None)
    stack: contextlib.AsyncExitStack = field(default_factory=contextlib.AsyncExitStack)

    async def __aenter__(self) -> RunContext:
        fw = self.agent.framework_config
        cfg = self.agent.config

        self.llm = _resolve_llm(self.agent)

        spill_store = cfg.spill_store
        if spill_store is None and getattr(fw.context, "spill_tool_results", False):
            from prodagent.cognition.context.budget import TokenCounter
            from prodagent.cognition.context.spill import ToolResultSpillStore

            spill_store = ToolResultSpillStore(counter=TokenCounter())
        self.spill_store = spill_store

        if fw.profile == "production":
            from prodagent.backends.factory import resolve_checkpoint, resolve_event_log

            self.checkpoint = cfg.checkpoint or resolve_checkpoint(fw)
            self.event_log = cfg.event_log or resolve_event_log(fw)
        else:
            # Bare kernel: explicit stores still work; None stays None —
            # nothing resolves, nothing hits disk.
            self.checkpoint = cfg.checkpoint
            self.event_log = cfg.event_log
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.stack.aclose()


# ── Run entry points — drive a fresh or resumed run to terminal state ─────────


async def drive_stream(
    agent: Agent,
    task: str,
    *,
    run_id: str | None = None,
    output_schema: type[BaseModel] | None = None,
    forced_mode: ExecutionMode | None = None,
    initial_messages: MessageList | None = None,
    parent_run_id: str | None = None,
) -> AsyncGenerator[AgentEvent, None]:
    """Stream agent events from a fresh or resumed run."""
    root_run_id = run_id or str(uuid.uuid4())
    initial_ctx = await _resolve_initial_context(agent, root_run_id, task)
    if parent_run_id is not None:
        initial_ctx.parent_run_id = parent_run_id
    loop = RunLoop(
        root_agent=agent,
        initial_ctx=initial_ctx,
        root_run_id=root_run_id,
        output_schema=output_schema,
        forced_mode=forced_mode,
        initial_messages=initial_messages,
    )
    async for event in loop.run():
        yield event


async def drive(
    agent: Agent,
    task: str,
    *,
    run_id: str | None = None,
    parent_run_id: str | None = None,
    forced_mode: ExecutionMode | None = None,
    initial_messages: MessageList | None = None,
) -> AgentRun:
    """Drive an agent to terminal state and return the final run. Used by spawn."""
    root_run_id = run_id or str(uuid.uuid4())
    stream = drive_stream(
        agent,
        task,
        run_id=root_run_id,
        forced_mode=forced_mode,
        initial_messages=initial_messages,
        parent_run_id=parent_run_id,
    )
    return await collect_final_run(stream, fallback_run_id=root_run_id, fallback_task=task)


async def collect_final_run(
    stream: AsyncGenerator[AgentEvent, None],
    *,
    fallback_run_id: str,
    fallback_task: str,
) -> AgentRun:
    final_run: AgentRun | None = None
    async for event in stream:
        if isinstance(event, (RunCompletedEvent, RunFailedEvent, RunSuspendedEvent)):
            final_run = event.run
    if final_run is None:
        return make_failed_run(fallback_run_id, fallback_task)
    return final_run


async def _resolve_initial_context(agent: Agent, root_run_id: str, task: str) -> RunContext:
    """Pick fresh-start vs peer-resume based on checkpoint state."""
    resume_peer = await find_suspended_peer(agent.checkpoint, root_run_id)
    if resume_peer is not None:
        return await _resume_peer_context(agent, root_run_id, resume_peer)
    return RunContext(agent=agent, task=task, run_id=root_run_id, depth=0)


async def _resume_peer_context(
    agent: Agent, root_run_id: str, resume_peer: tuple[str, str]
) -> RunContext:
    peer_name, peer_run_id = resume_peer
    peer_spec = agent.peer_named(peer_name)
    if peer_spec is None:
        logger.warning(
            "[orchestrator] suspended peer %r not on agent %r — falling back to fresh",
            peer_name,
            agent.name,
        )
        return RunContext(agent=agent, task="", run_id=root_run_id, depth=0)
    logger.info(
        "[orchestrator] resuming suspended peer %r (run_id=%s)",
        peer_name,
        peer_run_id,
    )
    return RunContext(
        agent=peer_spec.fork_as_peer(agent, root_run_id),
        task="",
        run_id=peer_run_id,
        depth=1,
    )


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
        self._peer_budget: BudgetLedger | None = (
            BudgetLedger(max=root_agent.budget_config) if root_agent.budget_config else None
        )
        self._relay_dedupe: IdempotentMessageHandler | None = None
        self._relay_pipe: Pipeline | None = None

    def _relay_pipeline(self) -> Pipeline:
        """Assembly pipeline for peer relays, built on first handoff.

        Dedupe is shared across the whole chain (one handler per RunLoop);
        hooks are read at first relay, when executor preparation has attached
        them. The PEER_HANDOFF audit event fires from the pipeline's last
        slot — only for crossings that were actually delivered.
        """
        if self._relay_pipe is None:
            fw = self._ctx.agent.framework_config
            orch = fw.orchestration if fw is not None else None
            ttl = orch.handoff_idempotency_ttl_s if orch is not None else 600.0
            self._relay_dedupe = IdempotentMessageHandler(ttl_seconds=ttl)
            self._relay_pipe = assembly_pipeline(
                dedupe=self._relay_dedupe,
                hooks=self._ctx.agent.hooks,
                audit_event=self._relay_audit_event,
            )
        return self._relay_pipe

    @staticmethod
    def _relay_audit_event(
        crossing: Crossing[Any],
    ) -> tuple[HookEvent, dict[str, Any]] | None:
        packet = crossing.payload
        task = getattr(packet, "task_description", "")
        return (
            HookEvent.PEER_HANDOFF,
            {
                "from_agent": crossing.from_agent,
                "to_agent": crossing.to,
                "task": task[:120] if task else "",
                "depth": crossing.meta.get("depth", 0),
                "parent_run_id": crossing.meta.get("parent_run_id"),
                "child_run_id": crossing.meta.get("child_run_id"),
            },
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
        if not handoff.message_id:
            handoff.message_id = str(uuid.uuid4())  # checkpoint written pre-migration
        peer_run_id = child_run_id(self._root_run_id, peer_name)
        handoff.peer_run_id = peer_run_id  # persist on the run before save below

        delivery = await self._relay_pipeline().process(
            Crossing.mint(
                direction=Direction.DOWNSTREAM,
                kind=CrossingKind.HANDOFF,
                from_agent=self._ctx.agent.name,
                to=peer_name,
                payload=packet,
                trace_id=self._root_run_id,
                message_id=handoff.message_id,
                depth=self._ctx.depth + 1,
                parent_run_id=self._ctx.run_id,
                child_run_id=peer_run_id,
            )
        )
        if delivery.status != "delivered":
            # A duplicate relay (checkpointed handoff replayed in-process) or a
            # gate veto — the chain stops here and the current run settles.
            logger.warning(
                "[orchestrator] handoff %s → %s not delivered (%s): %s",
                self._ctx.agent.name,
                peer_name,
                delivery.status,
                delivery.reason,
            )
            return None

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
            # The chain root's final output is an UPSTREAM crossing into the
            # caller's world — if the root agent declared an output contract,
            # it is admitted here, mirroring the structured-output gate above.
            contract = self._root_agent.config.output_contract
            if contract is not None and run.state is RunState.COMPLETED:
                settle_pipeline = admission_pipeline(contract=contract)
                delivery = await settle_pipeline.process(
                    Crossing.mint(
                        direction=Direction.UPSTREAM,
                        kind=CrossingKind.RESULT,
                        from_agent=self._root_agent.name,
                        to="caller",
                        payload={
                            "agent": self._root_agent.name,
                            "output": run.final_output or "",
                            "state": run.state.value,
                        },
                        trace_id=self._root_run_id,
                        message_id=f"{self._root_run_id}:settle",
                    )
                )
                if delivery.status == "rejected":
                    run.state = RunState.FAILED
                    run.last_error = f"contract violation: {delivery.reason}"
                    logger.warning(
                        "RunLoop[%s] root output rejected by contract: %s",
                        run.run_id,
                        delivery.reason,
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
                Gate.RUN_COMPLETE,
                run_id=run.run_id,
                final_output=run.final_output or "",
                run=run,
                turns=run.turn_count,
                cost_usd=run.cost_usd,
                state=run.state.value,
            )
        except SECURITY_VETO_EXCEPTIONS:
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
