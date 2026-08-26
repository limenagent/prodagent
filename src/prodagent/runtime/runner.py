"""RunLoop — the run driver: one agent hop at a time, chained across peers.

Lives in runtime (it drives the kernel's executors); collaboration
primitives and the messaging plane stay in coordination and reach the hop
only through the ``tool_assemblers`` seam on ``RunContext``.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import uuid
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from prodagent.kernel.budget import BudgetLedger
from prodagent.kernel.bus import HookEvent
from prodagent.kernel.events import RunCompletedEvent, RunFailedEvent, RunSuspendedEvent
from prodagent.kernel.state import AgentRun, is_child_subordinate, make_failed_run
from prodagent.runtime.compose import (
    find_suspended_peer,
    hop_tool_assemblers,
    make_settler,
    peer_relay,
)
from prodagent.runtime.factory import LeafExecutorFactory
from prodagent.runtime.parent_runtime import SpawnAccumulator

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator
    from typing import Protocol

    from pydantic import BaseModel

    from prodagent.cognition.context.spill import ToolResultSpillStore
    from prodagent.kernel.bus import HookRegistry
    from prodagent.kernel.events import AgentEvent
    from prodagent.kernel.types import ExecutionMode, MessageList
    from prodagent.ports import CheckpointStore, EventLog
    from prodagent.ports.budget_ledger import BudgetLedgerPort
    from prodagent.ports.llm import LLMClient
    from prodagent.runtime.agent import Agent
    from prodagent.runtime.parent_runtime import SpawnAccumulator

    class _Relay(Protocol):
        """What the loop needs from a peer relay — implemented by
        coordination/relay.py, named only through the compose seam."""

        async def next_context(
            self,
            ctx: RunContext,
            run: AgentRun,
            spawn_acc: SpawnAccumulator | None = None,
            ledger: BudgetLedgerPort | None = None,
        ) -> RunContext | None: ...


logger = logging.getLogger(__name__)


# ── RunContext — per-hop input, resolved into runtime dependencies on enter ──


def _resolve_llm(agent: Agent) -> LLMClient:
    from prodagent.backends.factory import resolve_llm
    from prodagent.runtime.compose import wrap_llm

    llm = agent.config.llm or resolve_llm(agent.framework_config)
    return wrap_llm(llm, agent.framework_config)


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
    budget_ledger: BudgetLedger | None = None
    """Chain-scoped shared ledger — one per RunLoop, set by the RunLoop itself."""

    tool_assemblers: list[Any] = field(default_factory=list)
    """Hop tool contributors (spawn/peer wrappers), attached by the driver.

    The factory consumes this blindly — which collaboration capabilities
    exist is coordination's business, not the runtime's."""

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

        # Bare kernel: explicit stores still work; None stays None —
        # nothing resolves, nothing hits disk.
        from prodagent.runtime.compose import resolve_checkpoint, resolve_event_log

        self.checkpoint = resolve_checkpoint(fw, cfg.checkpoint)
        self.event_log = resolve_event_log(fw, cfg.event_log)
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
    budget_ledger: BudgetLedger | None = None,
) -> AsyncGenerator[AgentEvent, None]:
    """Stream agent events from a fresh or resumed run.

    ``budget_ledger`` joins a spawned child into its parent's chain ledger —
    one accounting tree per root run."""
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
        budget_ledger=budget_ledger,
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
    budget_ledger: BudgetLedger | None = None,
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
        budget_ledger=budget_ledger,
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
    Not to be confused with :class:`~prodagent.kernel.loop.ReactiveLoop`,
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
        budget_ledger: BudgetLedger | None = None,
    ) -> None:
        self._root_agent = root_agent
        self._ctx = initial_ctx
        self._root_run_id = root_run_id
        self._output_schema = output_schema
        self._factory = LeafExecutorFactory(
            forced_mode=forced_mode, initial_messages=initial_messages
        )
        # One ledger for the whole tree: a spawned child arrives with its
        # parent's ledger (siblings visible); a root run mints a fresh one.
        self._ledger: BudgetLedger | None = budget_ledger or (
            BudgetLedger(max=root_agent.budget_config) if root_agent.budget_config else None
        )
        self._relay: _Relay | None = None  # built lazily via the compose seam

    async def run(self) -> AsyncGenerator[AgentEvent, None]:
        overall_final_run: AgentRun | None = None
        settle_hooks: HookRegistry | None = None

        while True:
            ctx = self._ctx
            ctx.budget_ledger = self._ledger
            if not ctx.tool_assemblers:
                ctx.tool_assemblers = hop_tool_assemblers()
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

                    next_ctx = await self._next_context(final_run, ctx, spawn_acc)
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
        spawn_acc: SpawnAccumulator | None = None,
    ) -> RunContext | None:
        """Hand off to the next peer hop via the relay (compose seam).

        The relay itself — budget settle-at-handoff, dedupe pipeline,
        checkpoint persistence, peer fork — lives with the peer primitive in
        ``coordination/relay.py``; runtime stays blind to coordination."""
        if run is None or run.pending_handoff is None:
            return None
        if self._relay is None:
            self._relay = peer_relay(self._root_run_id)
        relay = self._relay
        return await relay.next_context(ctx, run, spawn_acc, self._ledger)

    async def _settle(
        self,
        run: AgentRun,
        ctx: RunContext,
        hooks: HookRegistry | None,
    ) -> None:
        settler = make_settler(
            agent_name=self._root_agent.name,
            root_run_id=self._root_run_id,
            output_schema=self._output_schema,
            output_contract=self._root_agent.config.output_contract,
        )
        await settler.settle(run, ctx, hooks)

    async def _finalize_run(
        self,
        run: AgentRun | None,
        ctx: RunContext,
        hooks: HookRegistry | None,
        spawn_acc: SpawnAccumulator | None,
    ) -> None:
        if run is None:
            run = make_failed_run(ctx.run_id, ctx.task)

        if spawn_acc is not None:
            spawn_acc.fold_into(run)

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
            # Drain hooks live on the handler's instance; a bound method hides them.
            owner = getattr(handler, "__self__", None)
            flush = getattr(owner if owner is not None else handler, "flush", None)
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
