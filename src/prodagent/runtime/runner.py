"""The driver — the one runtime module that turns an Agent into events.

Owns, in order: the per-hop ``RunContext`` (services resolved on enter),
the entry points (``drive`` / ``drive_stream``), the ``RunLoop`` (exactly
one run, driven to its terminal event — a peer chain is IN the run, handoff
being a command the scheduler applies to the plan), and the per-hop
assembly (tools, engine, executor) the old compose.py used to own. Services
come from ``backends/factory``; collaboration tool doors live in
``runtime/tools``; delegation mechanics live in ``runtime/delegate``.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import uuid
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from prodagent.base.config import ContextConfig
from prodagent.base.errors import PermissionDenied, PlanAlreadyCompletedError
from prodagent.kernel.budget import SAFETY_NET_BUDGET, SpawnAccumulator, open_ledger
from prodagent.kernel.bus import Gate, HookEvent
from prodagent.kernel.run import (
    Run,
    collect_final_run,
    is_child_subordinate,
    make_failed_run,
)
from prodagent.kernel.types import RunCompletedEvent, RunFailedEvent, RunSuspendedEvent
from prodagent.runtime.recipes.agent_loop import AgentLoop
from prodagent.runtime.tools import hop_tool_assemblers
from prodagent.tooling.dispatcher import ToolDispatcher
from prodagent.tooling.merge import merge_tools_by_name

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator

    from pydantic import BaseModel

    from prodagent.cognition.context.spill import ToolResultSpillStore
    from prodagent.kernel.budget import BudgetLedger
    from prodagent.kernel.bus import HookRegistry
    from prodagent.kernel.types import AgentEvent, MessageList
    from prodagent.ports import CheckpointStore, EventLog, Executor
    from prodagent.ports.llm import LLMClient
    from prodagent.ports.persistence import BlobStore
    from prodagent.runtime.agent import Agent


logger = logging.getLogger(__name__)


# ── RunContext — per-hop input, resolved into runtime dependencies on enter ──


def _resolve_llm(agent: Agent) -> LLMClient:
    """Configured client or env-resolved default, then profile-wrapped —
    the one place a hop's LLM identity is decided."""
    from prodagent.backends.factory import resolve_llm, wrap_llm

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
    blob_store: BlobStore | None = field(init=False, default=None)
    spill_store: ToolResultSpillStore | None = field(init=False, default=None)
    stack: contextlib.AsyncExitStack = field(default_factory=contextlib.AsyncExitStack)
    budget_ledger: BudgetLedger | None = None
    """Chain-scoped shared ledger — one per RunLoop, set by the RunLoop itself."""

    tool_assemblers: list[Any] = field(default_factory=list)
    """Hop tool contributors (spawn/peer doors), attached by the driver.

    The assembly consumes this blindly — which collaboration capabilities
    exist is the assembler list's business, not the driver's."""

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
        from prodagent.backends.factory import (
            resolve_blob_store,
            resolve_checkpoint,
            resolve_event_log,
        )

        self.checkpoint = resolve_checkpoint(fw, cfg.checkpoint)
        self.event_log = resolve_event_log(fw, cfg.event_log)
        if self.event_log is None:
            # A hop that configures no WAL of its own still records when an
            # enclosing orchestration opened one — facts follow the
            # orchestration, not each sub-agent's config diligence.
            from prodagent.base.run_context import current_event_log

            self.event_log = current_event_log()
        # Spill target for oversized boundary facts — profile-decided,
        # like every service choice, in backends/factory.
        self.blob_store = resolve_blob_store(fw, cfg.blob_store, event_log=self.event_log)
        # Boundary recorder: with an event log configured,
        # every LLM answer this hop's client gives lands on the driving run's
        # boundary stream. One wrap point covers every execution shape —
        # they share this client. Off-scope calls (background distillation)
        # skip themselves inside the recorder.
        if self.event_log is not None:
            from prodagent.llm.recording import RecordingLLM

            if not isinstance(self.llm, RecordingLLM):
                from prodagent.llm.recording import RecordingLLMClient

                self.llm = RecordingLLMClient(
                    self.llm,
                    self.event_log,
                    blobs=self.blob_store,
                    threshold_bytes=fw.boundary_blob_threshold_bytes,
                )
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
    single_unit: bool = False,
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
        single_unit=single_unit,
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
    single_unit: bool = False,
    initial_messages: MessageList | None = None,
    budget_ledger: BudgetLedger | None = None,
) -> Run:
    """Drive an agent to terminal state and return the final run. Used by spawn."""
    root_run_id = run_id or str(uuid.uuid4())
    stream = drive_stream(
        agent,
        task,
        run_id=root_run_id,
        single_unit=single_unit,
        initial_messages=initial_messages,
        parent_run_id=parent_run_id,
        budget_ledger=budget_ledger,
    )
    return await collect_final_run(stream, fallback_run_id=root_run_id, fallback_task=task)


async def _resolve_initial_context(agent: Agent, root_run_id: str, task: str) -> RunContext:
    """The one hop's context — fresh by construction: a crashed or suspended
    run resumes inside the scheduler from its own checkpoint, so the driver
    never needs to discover where a chain went."""
    return RunContext(agent=agent, task=task, run_id=root_run_id, depth=0)


class RunLoop:
    """Drives exactly one run to its terminal event.

    Build the executor via ``SchedulerFactory``, stream it to a terminal
    event, finalize (fold spawn accounting, drain observers) and settle.
    Peer chains are IN the run — handoff is a command the scheduler applies
    to the plan — so there are no further hops to orchestrate. Not to be
    confused with the AgentLoop — the round loop inside an autonomous node;
    ``RunLoop`` never talks to an LLM directly.
    """

    def __init__(
        self,
        root_agent: Agent,
        initial_ctx: RunContext,
        root_run_id: str,
        output_schema: type[BaseModel] | None,
        *,
        single_unit: bool = False,
        initial_messages: MessageList | None = None,
        budget_ledger: BudgetLedger | None = None,
    ) -> None:
        self._root_agent = root_agent
        self._ctx = initial_ctx
        self._root_run_id = root_run_id
        self._output_schema = output_schema
        self._factory = SchedulerFactory(single_unit=single_unit, initial_messages=initial_messages)
        # One ledger for the whole tree: a spawned child arrives with its
        # parent's ledger (siblings visible); a root run mints a fresh one.
        self._ledger = open_ledger(root_agent.budget_config, existing=budget_ledger)

    async def run(self) -> AsyncGenerator[AgentEvent, None]:
        """Prepare executor → stream to a terminal event → finalize the run
        (fold spawn accounting, drain observers) → settle. Cancellation
        still finalizes — a killed run leaves a settled FAILED run on disk,
        never a dangling one."""
        ctx = self._ctx
        ctx.budget_ledger = self._ledger
        if not ctx.tool_assemblers:
            ctx.tool_assemblers = hop_tool_assemblers()
        final_run: Run | None = None
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
                    raise
                finally:
                    await self._finalize_run(final_run, ctx, hooks, spawn_acc)

                if final_run is not None:
                    settle_hooks = self._root_agent.hooks
                    if settle_hooks is None:
                        settle_hooks = self._root_agent.attach_default_hooks()
                    await self._settle(final_run, ctx.checkpoint, settle_hooks)
        except asyncio.CancelledError:
            raise

    async def _settle(
        self,
        run: Run,
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
        )
        await settler.settle(run, checkpoint, hooks)

    async def _finalize_run(
        self,
        run: Run | None,
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


# ── chat turns — the session-side machinery behind Agent.chat ────────────────


async def begin_chat_turn(
    agent: Agent,
    message: str,
    session_id: str,
    *,
    as_unit: bool = False,
) -> tuple[Any, str, bool, MessageList]:
    """Open a fresh turn: allocate the run id (a SUSPENDED predecessor is
    resumed instead — see ``ConversationSession.start_turn``), guard against
    an orphan checkpoint stealing the id, and persist the session before any
    work starts, so a crash mid-turn finds a resumable record."""
    from prodagent.base.errors import RunIdCollisionError
    from prodagent.base.session import ConversationSession
    from prodagent.kernel.types import RunState

    # A chat turn runs the agent itself as the unit — unless the agent
    # carries a preset graph (a bound Workflow), in which case the turn
    # runs that graph. Composition decides, not a mode enum.
    single_unit = as_unit or agent.config.initial_plan is None
    store = agent._ensure_session_store_resolved()  # noqa: SLF001 — driver owns session wiring
    session = await store.load(session_id)
    if session is None:
        session = ConversationSession(session_id=session_id, agent_id=agent.config.name)
    if (
        agent.config.initial_plan is not None
        and not single_unit
        and session.last_turn is not None
        and session.last_turn.state is not RunState.SUSPENDED
    ):
        raise PlanAlreadyCompletedError(session.last_turn.run_id)
    alloc = session.start_turn(message, single_unit=single_unit)

    if alloc.is_new:
        checkpoint = agent._ensure_checkpoint_resolved()  # noqa: SLF001
        if checkpoint is not None:
            orphan = await checkpoint.load(alloc.run_id)
            if orphan is not None:
                raise RunIdCollisionError(alloc.run_id)
        await store.save(session, expected_version=session.version)

    return session, alloc.run_id, alloc.single_unit, alloc.messages


async def load_suspended_turn(agent: Agent, session_id: str, store: Any) -> tuple[Any, str, bool]:
    """Resume a parked turn: SUSPENDED is the graceful resumable state;
    RUNNING is tolerated for hard crashes (kill -9 leaves no chance to
    suspend); a graceful stream close suspends the turn instead."""
    from prodagent.base.errors import PlanAlreadyCompletedError
    from prodagent.kernel.types import RunState

    session = await store.load(session_id)
    if session is None:
        raise PlanAlreadyCompletedError(f"<unknown:{session_id}>")
    if session.last_turn is None or session.last_turn.state not in (
        RunState.SUSPENDED,
        RunState.RUNNING,
    ):
        raise PlanAlreadyCompletedError(
            session.last_turn.run_id if session.last_turn else f"<{session_id}>"
        )
    return session, session.last_turn.run_id, session.last_turn.single_unit


def tape_prefixed(run_id: str) -> str:
    """Tape attribution for member turns: inside a multi-agent root scope,
    the session's turn id gains the ``<root>::`` prefix — the convention
    spawned children already follow, so one catalog entry holds the whole
    multi-agent run. Deterministic on resume."""
    from prodagent.base.run_context import current_tape_root

    root = current_tape_root()
    if root and not run_id.startswith(f"{root}::"):
        return f"{root}::{run_id}"
    return run_id


def find_approval_gate(agent: Agent) -> Any:
    """Locate the approval provider in wiring order: the bus's typed slot
    first, then an explicitly configured one."""
    from prodagent.hooks.approval import ApprovalProvider

    # Idempotent wire-first: what a probe sees is what a run would use.
    agent.attach_default_hooks()
    hooks = agent.config.hooks
    if hooks is not None:
        gate = hooks.require(ApprovalProvider)
        if gate is not None:
            return gate
    if isinstance(agent.config.approval, ApprovalProvider):
        return agent.config.approval
    return None


# ── one hop's assembly: tools, engine, executor ──────────────────────────────


def make_settler(
    agent_name: str,
    root_run_id: str,
    output_schema: Any,
    output_contract: Any = None,
) -> Any:
    """The chain-terminal discipline: validate the final output against the
    declared schema (when one exists) and let the driver checkpoint. A
    schema violation fails the run — the chain's last answer is its
    contract."""

    class _Settler:  # noqa: D101
        async def settle(self, run: Run, checkpoint: Any, hooks: HookRegistry | None) -> None:
            if (
                output_schema is not None
                and run.state is not None
                and run.final_output
                and hasattr(output_schema, "model_validate_json")
            ):
                try:
                    parsed = output_schema.model_validate_json(run.final_output)
                    if hasattr(parsed, "model_dump"):
                        run.structured_output = parsed
                except Exception as exc:  # noqa: BLE001 — the verdict is data
                    run.fail(f"output contract violation: {exc}")
                    return
            if checkpoint is not None:
                from prodagent.kernel.bus import save_and_fire_checkpoint

                await save_and_fire_checkpoint(checkpoint, run, hooks)

    return _Settler()


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


def subagent_invoker(ctx: Any) -> Any:
    """Delegation nodes' activation port: resolve the child on the parent's
    roster, activate through the shared
    ``activate_subagent`` core — the same core the spawn tool uses, so both
    entry points grow isomorphic Run trees."""
    from prodagent.runtime.delegate import activate_child

    agent = ctx.agent

    async def _invoke(child_name: str, task: str, run_id: str = "") -> dict[str, Any]:
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

        result = await activate_child(
            ctx,
            spec,
            task,
            parent_run_id=ctx.run_id,
            depth=ctx.depth + 1,
        )
        return asdict(result)

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
        # The chat path runs the agent itself as the body (one-node plan,
        # checkpoint-resumable); the work path runs a preset graph. An agent
        # without a preset IS its body at every hop — composition decides,
        # not a mode enum (plan-first lives in the examples, built from
        # kernel primitives: column 24).
        single_unit = (is_root and self._single_unit) or (agent.config.initial_plan is None)
        from prodagent.runtime.delegate import PEER_ENGINES_KEY, PeerEngines
        from prodagent.runtime.recipes.loop_body import LOOP_DRIVER_KEY, LoopBody
        from prodagent.runtime.recipes.react import DISPATCHER_KEY, LLM_CLIENT_KEY

        initial_messages = self._initial_messages if is_root else None

        # The peer roster's engines build lazily, on first handoff to that
        # peer — a handoff instantiates a peer node mid-run, and its body
        # asks THIS factory for the driver.
        peer_engines = PeerEngines(ctx)

        def _resolve_peer(name: str) -> Any:
            for peer in agent.config.peers:
                if peer.name == name:
                    return LoopBody(peer=name)
            return None

        # 3. the engine: one Scheduler for every shape.
        from prodagent.kernel.scheduler import Scheduler

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
            initial_body=LoopBody() if single_unit else None,
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
            dispatcher=dispatcher,
            # The loop recipe's seams: its driver rides the wiring bag
            # (bodies fetch collaborators by convention, the kernel just
            # carries them), the peer engines build on first handoff, and
            # the terminal marker is a callback the kernel fires at every
            # stream end of the single-body shape.
            wiring={
                LOOP_DRIVER_KEY: engine,
                LLM_CLIENT_KEY: ctx.llm,
                DISPATCHER_KEY: dispatcher,
                PEER_ENGINES_KEY: peer_engines,
            },
            terminal_marker=engine.record_terminal if single_unit else None,
            resolve_peer=_resolve_peer,
            depth=ctx.depth,
            fns=agent.config.node_fns,
            llm_invoker=_llm_invoker(ctx.llm, hooks),
            subagent=subagent_invoker(ctx),
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
