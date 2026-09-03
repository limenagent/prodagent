"""compose — the assembly root: the only place that reads ``profile``.

``production()`` (core/config.py) flips flags; this module is the consumer
side — the one file that answers "what does a production agent consist of".

Capabilities attach through three sockets, and everything the framework
does uses one of them:

- **Port replacement** — implement a kernel/ports protocol: LLM adapters,
  the caching wrapper, the context assembler, every store backend.
- **Bus attachment** — register on the kernel bus: observers, gates,
  injectors (memory recall, approval veto, spans, learning).
- **Executor replacement** — implement ``Executor``: the default is the
  second strategy for iterating the Round atom.

Tools arrive through the hop seam (``tool_assemblers``); capabilities are
found via the bus's typed slots (``provide``/``require``).
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import asdict, dataclass, field
from typing import TYPE_CHECKING, Any

from prodagent.base.config import ContextConfig
from prodagent.base.errors import PermissionDenied
from prodagent.kernel.budget import SAFETY_NET_BUDGET
from prodagent.kernel.bus import Gate, HookEvent
from prodagent.kernel.types import ToolResult
from prodagent.runtime.recipes.agent_loop import AgentLoop
from prodagent.tooling.dispatcher import ToolDispatcher
from prodagent.tooling.merge import merge_tools_by_name

if TYPE_CHECKING:
    from prodagent.base.config import FrameworkConfig
    from prodagent.hooks.bundles.base import HookBundle
    from prodagent.kernel.budget import SpawnAccumulator
    from prodagent.kernel.bus import HookRegistry
    from prodagent.kernel.run import Run
    from prodagent.kernel.types import MessageList
    from prodagent.ports import CheckpointStore, EventLog, Executor, SessionStore
    from prodagent.ports.execution import HandoffActivation
    from prodagent.ports.llm import LLMClient
    from prodagent.ports.persistence import BlobStore
    from prodagent.runtime.agent import Agent
    from prodagent.runtime.runner import RunContext

logger = logging.getLogger(__name__)


def resolve_blob_store(
    fw: FrameworkConfig, explicit: BlobStore | None, *, event_log: EventLog | None
) -> BlobStore | None:
    """Spill target for oversized boundary facts. Production with an event
    log: the file blob store (big bodies belong on disk). Bare or log-less:
    ``None`` — facts stay inline (bare records nothing anyway)."""
    if explicit is not None:
        return explicit
    if fw.profile != "production" or event_log is None:
        return None
    from prodagent.backends.file.blob import FileBlobStore

    return FileBlobStore(fw.blobs_dir)


def wrap_llm(llm: LLMClient, fw: FrameworkConfig) -> LLMClient:
    """production(): wrap in the response cache. Bare: return as-is — a
    prompt cache is an optimization with observability side effects, not
    part of the loop."""
    if fw.profile != "production":
        return llm
    from prodagent.llm.cache import CachingLLM, CachingLLMClient

    if isinstance(llm, CachingLLM):
        return llm
    return CachingLLMClient(llm, framework_config=fw)


def resolve_checkpoint(
    fw: FrameworkConfig, explicit: CheckpointStore | None
) -> CheckpointStore | None:
    if fw.profile != "production":
        return explicit
    if explicit is not None:
        return explicit
    from prodagent.backends.factory import resolve_checkpoint as _resolve

    return _resolve(fw)


def resolve_event_log(fw: FrameworkConfig, explicit: EventLog | None) -> EventLog | None:
    if fw.profile != "production":
        return explicit
    if explicit is not None:
        return explicit
    from prodagent.backends.factory import resolve_event_log as _resolve

    return _resolve(fw)


def resolve_session_store(fw: FrameworkConfig, explicit: SessionStore | None) -> SessionStore:
    if explicit is not None:
        return explicit
    if fw.profile != "production":
        from prodagent.backends.factory import in_memory_session_store

        return in_memory_session_store()
    from prodagent.backends.factory import resolve_session_store as _resolve

    return _resolve(fw)


def hop_tool_assemblers() -> list[Any]:
    """Collaboration capabilities that contribute hop tools (spawn/peer).

    The driver attaches these to ``RunContext.tool_assemblers``; the factory
    consumes them blind. As the assembly root, this is the one place runtime
    may name coordination capabilities."""
    from prodagent.runtime.compose import assemble_peer_tools, assemble_spawn_tools

    return [assemble_spawn_tools, assemble_peer_tools]


DEFAULT_TIMEOUT_S = 600.0
_MAX_CHAIN_HOPS = 8
"""The transfer TTL (column 28): a peer chain longer than this is a
hand-off loop wearing a work costume — it stops here, loudly."""


async def find_suspended_peer(checkpoint: Any, root_run_id: str) -> tuple[str, str] | None:
    """Resume discovery — a chain parked mid-relay names its next hop in the
    root's checkpoint (the kernel parked ``PendingHandoff`` there). Returns
    ``(peer_name, peer_run_id)`` — or None for a fresh start."""
    if checkpoint is None:
        return None
    stored = await checkpoint.load(root_run_id)
    if stored is None or stored.pending_handoff is None:
        return None
    pending = stored.pending_handoff
    return pending.peer_name, pending.peer_run_id or root_run_id


class _Relay:
    """The relay the driver drives — lowers a parked Handoff into the next
    hop's activation (pure interpretation; the kernel parked the data)."""

    def __init__(self, root_run_id: str) -> None:
        self._root_run_id = root_run_id

    async def next_hop(
        self,
        agent: Any,
        run: Any,
        *,
        run_id: str,
        depth: int,
        checkpoint: Any,
        event_log: Any = None,
        spawn_acc: Any = None,
        ledger: Any = None,
    ) -> Any:
        return await relay_next_hop(agent, run, depth=depth, checkpoint=checkpoint)


def peer_relay(root_run_id: str) -> _Relay:
    """The peer-chain relay, as the driver's seam."""
    return _Relay(root_run_id)


@dataclass
class ChildResult:
    """Structured result of a child run — the dict SubPlanBody folds."""

    agent: str
    state: str
    output: str = ""
    turns: int = 0
    cost_usd: float = 0.0
    input_tokens: int = 0
    output_tokens: int = 0
    tool_history: list[Any] = field(default_factory=list)
    approval_request_id: str = ""
    failed_reason: str | None = None


async def activate_child(
    ctx: Any,
    agent: Agent,
    task: str,
    *,
    parent_run_id: str | None,
    depth: int,
    child_run_id: str | None = None,
    default_timeout_s: float = DEFAULT_TIMEOUT_S,
) -> ChildResult:
    """Activate one child — forked under this hop's wiring — and fold its
    terminal run. The wall-clock clamp is the child's own budget's seconds
    axis, not a guess; a timeout is a *result*, not an exception."""
    from prodagent.kernel.run import child_run_id as mint_child_id

    run_id = child_run_id or mint_child_id(parent_run_id or "", agent.name)
    budget_s = getattr(agent.budget_config, "max_seconds", None)
    timeout = min(default_timeout_s, budget_s) if budget_s else default_timeout_s
    try:
        activation_run = await asyncio.wait_for(
            _drive_child(ctx, agent, task, run_id, parent_run_id, depth),
            timeout=timeout,
        )
    except TimeoutError:
        return ChildResult(
            agent=agent.name,
            state="failed",
            failed_reason=f"child ran past its clock ({timeout:.0f}s)",
        )
    return _fold(agent.name, run_id, activation_run)


async def _drive_child(
    ctx: Any,
    agent: Agent,
    task: str,
    run_id: str,
    parent_run_id: str | None,
    depth: int,
) -> Run:
    """Drive the child to its terminal run — fork under this hop's wiring,
    then drive. The terminal event carries the run."""
    from prodagent.kernel.run import collect_final_run
    from prodagent.runtime.runner import drive_stream

    forked = agent.fork_as_spawn(ctx)
    return await collect_final_run(
        drive_stream(
            forked,
            task,
            run_id=run_id,
            parent_run_id=parent_run_id,
            budget_ledger=ctx.budget_ledger,
        ),
        fallback_run_id=run_id,
        fallback_task=task,
    )


def _fold(agent_name: str, run_id: str, run: Run) -> ChildResult:
    from prodagent.kernel.types import RunState

    if run.state is RunState.SUSPENDED:
        return ChildResult(
            agent=agent_name,
            state="suspended",
            output=run.final_output or "",
            approval_request_id=run.pending_approval_id or "",
            turns=run.metrics.turn_count,
            cost_usd=run.metrics.cost_usd,
            input_tokens=run.metrics.input_tokens,
            output_tokens=run.metrics.output_tokens,
        )
    if run.state is RunState.COMPLETED:
        return ChildResult(
            agent=agent_name,
            state="completed",
            output=run.final_output or "",
            turns=run.metrics.turn_count,
            cost_usd=run.metrics.cost_usd,
            input_tokens=run.metrics.input_tokens,
            output_tokens=run.metrics.output_tokens,
        )
    return ChildResult(
        agent=agent_name,
        state="failed",
        failed_reason=run.last_error or "child ended without completing",
        turns=run.metrics.turn_count,
        cost_usd=run.metrics.cost_usd,
    )


# ── call: the roster as one tool ─────────────────────────────────────────────


def _tool(name: str, fn: Any, description: str) -> Any:
    """A FunctionTool via the standard inference path (annotations → schema)."""
    from prodagent.kernel.types import SideEffectLevel, ToolMeta
    from prodagent.tooling.base import FunctionTool
    from prodagent.tooling.decorator import _infer_schema

    meta = ToolMeta(name=name, side_effect_level=SideEffectLevel.LOW, is_readonly=True)
    return FunctionTool(name=name, fn=fn, meta=meta, schema=_infer_schema(fn, name, description))


def assemble_spawn_tools(
    ctx: Any,
    active_tools: list[Any],
    tool_schemas: list[dict[str, Any]],
    spawn_acc: Any = None,
) -> Any:
    """One ``spawn_agent`` tool exposing the roster — the call mechanism's
    tool-shaped door. The model picks a name and a task; governance is the
    application's to compose. ``spawn_acc`` is passed through (governance
    retired with the message plane); the 4-arg signature is the assembler
    seam the factory calls."""
    agent = ctx.agent
    roster = [a.name for a in agent.child_agents]
    if not roster:
        return spawn_acc

    async def _spawn_agent(name: str, task: str) -> dict[str, Any]:
        child = next((a for a in agent.child_agents if a.name == name), None)
        if child is None:
            return {
                "error": "unknown_agent",
                "reason": "tool_not_available",
                "message": f"Unknown agent {name!r}. Available: {roster}",
            }
        result = await activate_child(
            ctx,
            child,
            task,
            parent_run_id=ctx.run_id,
            depth=ctx.depth + 1,
        )
        return asdict(result)

    agent_lines = "\n".join(
        f"  - {a.name}: {a.config.description or a.name}" for a in agent.child_agents
    )
    description = (
        "Delegate a sub-task to a specialised sub-agent and return its result.\n"
        f"Available sub-agents:\n{agent_lines}"
    )
    tool = _tool("spawn_agent", _spawn_agent, description)
    active_tools.append(tool)
    tool_schemas.append(tool.schema)
    return spawn_acc


# ── transfer: handoff tools + the thin relay ─────────────────────────────────


def assemble_peer_tools(
    ctx: Any,
    active_tools: list[Any],
    tool_schemas: list[dict[str, Any]],
    spawn_acc: Any = None,
) -> Any:
    """One ``handoff_to_<peer>`` tool per peer — transfer's tool door.
    ``spawn_acc`` is passed through (the 4-arg assembler seam)."""
    agent = ctx.agent
    for peer in agent.config.peers or []:
        _add_handoff_tool(agent, peer.name, active_tools, tool_schemas)
    return spawn_acc


def _add_handoff_tool(
    agent: Agent, peer_name: str, active_tools: list[Any], tool_schemas: list[dict[str, Any]]
) -> None:
    async def _handoff(task: str) -> ToolResult:
        return ToolResult.for_handoff(peer=peer_name, task=task, tool=f"handoff_to_{peer_name}")

    tool = _tool(
        f"handoff_to_{peer_name}",
        _handoff,
        f"Hand the conversation to peer {peer_name!r}; it continues the chain.",
    )
    active_tools.append(tool)
    tool_schemas.append(tool.schema)


async def relay_next_hop(
    agent: Agent,
    run: Run,
    *,
    depth: int,
    checkpoint: CheckpointStore | None,
) -> HandoffActivation | None:
    """Lower a parked handoff into the next hop's activation.

    Pure interpretation: the kernel parked ``PendingHandoff`` on the run;
    this resolves where the chain continues (minting the peer's run id) and
    persists the handing-off run so a crash mid-chain resumes at the relay.
    The hop cap is the transfer TTL."""
    pending = run.pending_handoff
    if pending is None:
        return None
    if depth >= _MAX_CHAIN_HOPS:
        logger.error(
            "[relay] chain exceeded %d hops (a hand-off loop?) — stopping at %s",
            _MAX_CHAIN_HOPS,
            pending.peer_name,
        )
        return None
    if checkpoint is not None:
        from prodagent.kernel.bus import save_and_fire_checkpoint

        await save_and_fire_checkpoint(checkpoint, run, None)
    from prodagent.ports.execution import HandoffActivation

    return HandoffActivation(
        peer_name=pending.peer_name,
        task=pending.task,
        run_id=pending.peer_run_id or "",
        parent_run_id=run.parent_run_id,
        depth=depth + 1,
    )


# ── the chain's end: settle ──────────────────────────────────────────────────


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


def default_bundles(fw: FrameworkConfig | None) -> list[HookBundle]:
    """The profile's bundle manifest — what ``attach_default_hooks`` wires.

    The bare profile stays silent: console is opt-in via env/flag, learning
    only attaches when ``skills=`` is set — no observer, no span export, no
    approval gate. The production profile restores the full stack."""
    from prodagent.hooks.bundles.default_wiring import (
        ApprovalDefaultBundle,
        CacheMonitorDefaultBundle,
        ConsoleDefaultBundle,
        LearningDefaultBundle,
        SpanDefaultBundle,
    )

    if fw is None or fw.profile == "bare":
        return [ConsoleDefaultBundle(), LearningDefaultBundle()]
    return [
        ConsoleDefaultBundle(),
        CacheMonitorDefaultBundle(),
        SpanDefaultBundle(),
        ApprovalDefaultBundle(),
        LearningDefaultBundle(),
    ]


# ── SchedulerFactory — building one hop's Scheduler (folded here from
# factory.py: the assembly root owns construction) ──


def _subagent_invoker(ctx: Any) -> Any:
    """Delegation nodes' activation port: resolve the child on the parent's
    roster, activate through the shared
    ``activate_subagent`` core — the same core the spawn tool uses, so both
    entry points grow isomorphic Run trees."""
    from prodagent.runtime.compose import activate_child

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
        # The chat path runs the agent itself as the body (one-node plan,
        # checkpoint-resumable); the work path runs a preset graph. An agent
        # without a preset IS its body at every hop — composition decides,
        # not a mode enum (plan-first lives in the examples, built from
        # kernel primitives: column 24).
        single_unit = (is_root and self._single_unit) or (agent.config.initial_plan is None)
        from prodagent.runtime.recipes.loop_body import LOOP_DRIVER_KEY, LoopBody
        from prodagent.runtime.recipes.react import DISPATCHER_KEY, LLM_CLIENT_KEY

        initial_messages = self._initial_messages if is_root else None

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
            # The loop recipe's two seams: its driver rides the wiring bag
            # (bodies fetch collaborators by convention, the kernel just
            # carries them), and its terminal marker is a callback the
            # kernel fires at every stream end of the single-body shape.
            wiring={
                LOOP_DRIVER_KEY: engine,
                LLM_CLIENT_KEY: ctx.llm,
                DISPATCHER_KEY: dispatcher,
            },
            terminal_marker=engine.record_terminal if single_unit else None,
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
