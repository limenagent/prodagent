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
from dataclasses import dataclass, field, replace
from typing import TYPE_CHECKING, Any, cast

from prodagent.kernel.budget import SpawnAccumulator, open_ledger
from prodagent.kernel.bus import HookEvent
from prodagent.kernel.state import (
    AgentRun,
    collect_final_run,
    is_child_subordinate,
    make_failed_run,
)
from prodagent.kernel.types import RunCompletedEvent, RunFailedEvent, RunSuspendedEvent
from prodagent.ports.execution import InProcessChatRunner
from prodagent.runtime.compose import (
    find_suspended_peer,
    hop_tool_assemblers,
    make_settler,
    peer_relay,
)
from prodagent.runtime.factory import LeafExecutorFactory
from prodagent.runtime.parent_runtime import ParentRuntime

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator
    from typing import Protocol

    from pydantic import BaseModel

    from prodagent.cognition.context.spill import ToolResultSpillStore
    from prodagent.kernel.budget import BudgetLedger
    from prodagent.kernel.bus import HookRegistry
    from prodagent.kernel.types import AgentEvent, ExecutionMode, MessageList
    from prodagent.ports import CheckpointStore, EventLog
    from prodagent.ports.budget_ledger import BudgetLedgerPort
    from prodagent.ports.execution import (
        AgentActivation,
        HandoffActivation,
        RunnerPort,
    )
    from prodagent.ports.llm import LLMClient
    from prodagent.runtime.agent import Agent

    class _Relay(Protocol):
        """What the loop needs from a peer relay — implemented by
        coordination/relay.py, named only through the compose seam. The relay
        returns a pure-data ``HandoffActivation``; interpreting it (peer
        lookup, fork, hop context) is this driver's job."""

        async def next_hop(
            self,
            agent: Agent,
            run: AgentRun,
            *,
            run_id: str,
            depth: int,
            checkpoint: CheckpointStore | None,
            event_log: EventLog | None,
            spawn_acc: SpawnAccumulator | None = None,
            ledger: BudgetLedgerPort | None = None,
        ) -> HandoffActivation | None: ...


logger = logging.getLogger(__name__)


# ── RunContext — per-hop input, resolved into runtime dependencies on enter ──


def _resolve_llm(agent: Agent) -> LLMClient:
    """Configured client or env-resolved default, then profile-wrapped —
    the one place a hop's LLM identity is decided."""
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

    runner: RunnerPort | None = None
    """The hop's execution seam, set by RunLoop after context entry (stores
    resolved) and before executor preparation. Spawn/peer tool assemblers
    consume it — coordination reaches execution only through the port."""

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


# ── InProcessRunner — the RunnerPort's in-process implementation ──────────────


class InProcessRunner:
    """One agent activation, executed right here.

    Bound to the hop's wiring (a :class:`ParentRuntime`), a bare run forks
    its target under that wiring — the fork is runtime vocabulary, the port
    contract stays pure execution. Unbound (``runtime=None``) the target runs
    as-is: the standalone default for callers outside a hop chain. Chat
    activations (``session_id`` set) never fork — a member speaks as itself.
    """

    def __init__(self, runtime: ParentRuntime | None = None) -> None:
        self._runtime = runtime

    def activate(self, activation: AgentActivation) -> AsyncGenerator[AgentEvent, None]:
        if activation.session_id is not None:
            # Session-scoped turns delegate to the chat runner, the local
            # default that owns that semantics — this used to be a copy of it.
            return InProcessChatRunner().activate(activation)
        agent = activation.agent
        if self._runtime is not None:
            runtime = replace(
                self._runtime,
                # Chain budget wins if declared; otherwise the child's own
                # config supplies the ceiling (never both — no double cap).
                budget=self._runtime.budget or agent.budget_config,
            )
            agent = agent.fork_as_spawn(runtime)
        return drive_stream(
            agent,
            activation.task,
            run_id=activation.run_id,
            parent_run_id=activation.parent_run_id,
            budget_ledger=cast("BudgetLedger | None", activation.budget_ledger),
        )


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


async def _resolve_initial_context(agent: Agent, root_run_id: str, task: str) -> RunContext:
    """Pick fresh-start vs peer-resume based on checkpoint state."""
    resume_peer = await find_suspended_peer(agent.checkpoint, root_run_id)
    if resume_peer is not None:
        return await _resume_peer_context(agent, root_run_id, resume_peer)
    return RunContext(agent=agent, task=task, run_id=root_run_id, depth=0)


async def _resume_peer_context(
    agent: Agent, root_run_id: str, resume_peer: tuple[str, str]
) -> RunContext:
    """Rebuild the hop context for a chain that was parked mid-relay: the
    named peer forks under the root's wiring and continues under its own
    suspended run id — crash recovery for multi-hop runs."""
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
        self._ledger = open_ledger(root_agent.budget_config, existing=budget_ledger)
        self._relay: _Relay | None = None  # built lazily via the compose seam

    async def run(self) -> AsyncGenerator[AgentEvent, None]:
        """Hop loop: prepare executor → stream to a terminal event →
        finalize the hop (fold spawn accounting, mark peer continuations,
        drain observers) → relay a handoff into the next hop, or settle the
        whole chain. Cancellation still finalizes — a killed chain leaves a
        settled FAILED run on disk, never a dangling one."""
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
                    if ctx.runner is None:
                        ctx.runner = InProcessRunner(ParentRuntime.from_context(ctx))
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
                            # Hops after the first belong to the chain, not to
                            # a spawned subordinate — settlement keys on this.
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
                        await self._settle(overall_final_run, ctx.checkpoint, settle_hooks)
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

        The relay decides whether and where the chain continues and returns a
        pure-data :class:`~prodagent.ports.execution.HandoffActivation`;
        interpreting it — peer lookup, fork, hop context — is this driver's
        job, so coordination never constructs runtime objects. Budget
        settle-at-handoff, dedupe pipeline, and checkpoint persistence stay
        with the relay in ``coordination/relay.py``."""
        if run is None or run.pending_handoff is None:
            return None
        if self._relay is None:
            self._relay = peer_relay(self._root_run_id)
        relay = self._relay
        hop = await relay.next_hop(
            ctx.agent,
            run,
            run_id=ctx.run_id,
            depth=ctx.depth,
            checkpoint=ctx.checkpoint,
            event_log=ctx.event_log,
            spawn_acc=spawn_acc,
            ledger=self._ledger,
        )
        if hop is None:
            return None
        peer_spec = ctx.agent.peer_named(hop.peer_name)
        if peer_spec is None:
            logger.error(
                "[orchestrator] relay handed off to unknown peer %r — chain stops",
                hop.peer_name,
            )
            return None
        return RunContext(
            agent=peer_spec.fork_as_peer(
                ctx.agent,
                ctx.run_id,
                checkpoint=ctx.checkpoint,
                event_log=ctx.event_log,
            ),
            task=hop.task,
            run_id=hop.run_id,
            depth=hop.depth,
            parent_run_id=hop.parent_run_id,
        )

    async def _settle(
        self,
        run: AgentRun,
        checkpoint: Any,
        hooks: HookRegistry | None,
    ) -> None:
        """Chain-terminal settlement through the compose seam — the settler
        (coordination's) owns output-schema validation, contract checks and
        the final checkpoint; this driver only knows when the chain is over."""
        settler = make_settler(
            agent_name=self._root_agent.name,
            root_run_id=self._root_run_id,
            output_schema=self._output_schema,
            output_contract=self._root_agent.config.output_contract,
        )
        await settler.settle(run, checkpoint, hooks)

    async def _finalize_run(
        self,
        run: AgentRun | None,
        ctx: RunContext,
        hooks: HookRegistry | None,
        spawn_acc: SpawnAccumulator | None,
    ) -> None:
        """Per-hop wrap-up: a bare run becomes a synthetic FAILED one (no
        dangling state), spawn accounting folds into the run, SESSION_END
        fires, and background observers flush — except subordinate children,
        whose drained output folds upward instead of writing separately."""
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
                continue  # not every SESSION_END listener has buffers to drain
            if is_child_subordinate(run):
                continue  # children fold upward; their spans ride the parent's flush
            try:
                await flush()
            except Exception as exc:  # noqa: BLE001 — best-effort drain
                logger.warning(
                    "RunLoop._finalize_run: background flush failed for %r: %s",
                    handler,
                    exc,
                )
