"""Delegation — how one agent's work becomes another agent's execution.

Three mechanisms, one file: spawned children (``activate_child`` — a real
child run whose result folds back), peer hops (``PeerEngines`` — the peer's
loop engine, built lazily on first handoff), and the fork rules that derive
either from an ``Agent`` declaration. Lives in runtime because it composes
kernel bodies with agents and services; the kernel only sees the
``SubagentInvoker`` slot and ``Handoff`` commands.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from prodagent.kernel.budget import SAFETY_NET_BUDGET
from prodagent.runtime.recipes.agent_loop import AgentLoop
from prodagent.tooling.dispatcher import ToolDispatcher
from prodagent.tooling.merge import merge_tools_by_name

if TYPE_CHECKING:
    from prodagent.kernel.run import Run
    from prodagent.runtime.agent import Agent
    from prodagent.runtime.runner import RunContext

logger = logging.getLogger(__name__)

PEER_ENGINES_KEY = "peer_engines"
"""The wiring-bag key the composition root registers ``PeerEngines`` under —
a peer's ``LoopBody`` asks this factory for its driver on first handoff."""

__all__ = [
    "ChildResult",
    "PEER_ENGINES_KEY",
    "SpawnAccumulator",
    "PeerEngines",
    "activate_child",
    "fork_as_peer",
    "fork_as_spawn",
]


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
    default_timeout_s: float = 600.0,
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
) -> Any:
    """Drive the child to its terminal run — fork under this hop's wiring,
    then drive. The terminal event carries the run."""
    from prodagent.kernel.run import collect_final_run
    from prodagent.runtime.runner import drive_stream

    forked = fork_as_spawn(agent, ctx)
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


def _fold(agent_name: str, run_id: str, run: Any) -> ChildResult:
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


# ── fork rules — deriving an Agent under someone else's wiring ────────────────


def fork_as_spawn(agent: Agent, ctx: Any) -> Agent:
    """Fork as a spawned child: takes the parent's wiring wholesale —
    a child has no peers of its own to preserve."""
    return agent._fork(  # noqa: SLF001 — the fork family owns Agent's internals
        **agent._runtime_overrides(ctx),  # noqa: SLF001
        extensions=list(agent.config.extensions),
        injectors=list(agent.config.injectors),
        checkers=list(agent.config.checkers),
        event_handlers=list(agent.config.event_handlers),
        mcp=list(agent.config.mcp),
    )


def fork_as_peer(
    peer: Agent,
    parent: Agent,
    parent_run_id: str | None,
    *,
    checkpoint: Any = None,
    event_log: Any = None,
) -> Agent:
    """Fork ``peer`` as the next link of a chain: the fork runs under the
    *parent's* wiring (hooks, extensions, stores) but keeps its own peers —
    the chain can continue past it."""
    overrides = peer._runtime_overrides(parent)  # noqa: SLF001
    if checkpoint is not None:
        overrides["checkpoint"] = checkpoint
    if event_log is not None:
        overrides["event_log"] = event_log
    forked = peer._fork(  # noqa: SLF001
        **overrides,
        extensions=list(parent.config.extensions),
        injectors=list(parent.config.injectors),
        checkers=list(parent.config.checkers),
        event_handlers=list(parent.config.event_handlers),
        mcp=list(parent.config.mcp),
        peers=list(peer.config.peers),
    )
    forked._hooks_wired = parent._hooks_wired  # noqa: SLF001
    return forked


# ── peer engines — the graph-native handoff's execution seam ─────────────────


class PeerEngines:
    """The peer roster's loop engines, built lazily on first handoff.

    A chain hop is the same machinery one node over: the peer forks under
    THIS hop's wiring and gets the root engine's construction (dispatcher,
    tools, loop config). The scheduler instantiates the peer's node in the
    same plan; the engine only ever drives the one shared run. Peers never
    reached cost nothing; a peer cycle needs no cycle-breaker — engines
    build per name, once."""

    def __init__(self, ctx: Any) -> None:
        self._ctx = ctx
        self._engines: dict[str, Any] = {}
        self._locks: dict[str, asyncio.Lock] = {}

    async def driver_for(self, name: str) -> Any:
        """The named peer's engine, built on first request."""
        peer = self._find_peer(name)
        if peer is None:
            raise ValueError(
                f"handoff: peer {name!r} is not reachable from agent "
                f"{self._ctx.agent.name!r}'s roster (declare it as a peer, "
                "directly or on a peer of a peer)"
            )
        if name not in self._engines:
            lock = self._locks.setdefault(name, asyncio.Lock())
            async with lock:
                if name not in self._engines:  # lost the race — recheck
                    fork = fork_as_peer(
                        peer,
                        self._ctx.agent,
                        self._ctx.run_id,
                        checkpoint=self._ctx.checkpoint,
                        event_log=self._ctx.event_log,
                    )
                    self._engines[name] = await _build_loop_driver(fork, self._ctx)
        return self._engines[name]

    def _find_peer(self, name: str) -> Agent | None:
        """Roster-closure lookup by name — declarations only, no building."""
        ctx = self._ctx
        seen: set[str] = {ctx.agent.name}
        frontier = list(ctx.agent.config.peers)
        while frontier:
            peer: Agent = frontier.pop(0)
            if peer.name == name:
                return peer
            if peer.name in seen:
                continue
            seen.add(peer.name)
            frontier.extend(peer.config.peers)
        return None


async def _build_loop_driver(peer: Agent, ctx: RunContext) -> Any:
    """One peer's AgentLoop — the root engine's construction, narrowed:
    no preset plan, no terminal marker (the peer node is the marker)."""
    fw = peer.framework_config
    active_tools = await peer.resolve_tools()
    if peer.mcp_configs:
        from prodagent.mcp.registry import MCPRegistry

        try:
            registry = await ctx.stack.enter_async_context(MCPRegistry(peer.mcp_configs))
            merge_tools_by_name(active_tools, await registry.get_tools() or [])
        except Exception as exc:  # noqa: BLE001 — a peer's MCP is best-effort
            logger.warning("peer %r MCP registry setup failed: %s", peer.name, exc)
    tool_schemas = [t.schema for t in active_tools]
    dispatcher = ToolDispatcher(
        {t.name: t for t in active_tools},  # type: ignore[misc]
        tool_registry=peer.config.tool_registry,
        hooks=peer.hooks,
        skills=peer.skills,
        agent_id=peer.name,
        agent_name=peer.name,
        event_log=ctx.event_log,
        blob_store=ctx.blob_store,
        blob_threshold_bytes=fw.boundary_blob_threshold_bytes,
    )
    system = peer.build_system_prompt()
    return AgentLoop(
        ctx.llm,
        dispatcher,
        system_prompt=system,
        tools_schema=tool_schemas,
        budget=peer.budget_config or SAFETY_NET_BUDGET,
        context_manager=peer.build_context_manager(system, fw, ctx),
        hooks=peer.hooks,
        loop_config=fw.loop,
        checkpoint_store=ctx.checkpoint,
        event_log=ctx.event_log,
        spill_store=ctx.spill_store,
        budget_ledger=ctx.budget_ledger,
    )


# ── Spawn accounting — the fold side of the settlement arithmetic ─────────────
# Spawn accounting is delegation's concept: child spend that must land on the
# parent's persisted Run.metrics at hop end. The enforcement view is the
# kernel's BudgetLedger; this section is the metrics/transcript fold.


def fold_spawn_fields(target: Any, source: Any) -> None:
    """Add source's flat spawn-accounting fields onto target, in place."""
    target.cost_usd += source.cost_usd
    target.input_tokens += source.input_tokens
    target.output_tokens += source.output_tokens
    if source.tool_history:
        target.tool_history.extend(source.tool_history)


@dataclass
class SpawnAccumulator:
    """Shared sink for sub-agent spend so parent runs can reconcile cost.

    The enforcement view is the shared kernel ``BudgetLedger``; this
    accumulator is the metrics/transcript fold sink — child spend that must
    land on the parent's persisted ``Run.metrics`` at hop end.
    """

    cost_usd: float = 0.0
    turns: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    spawn_count: int = 0
    tool_history: list[Any] = field(default_factory=list)

    def add(self, result: Any) -> None:
        fold_spawn_fields(self, result)
        self.turns += result.turns
        self.spawn_count += 1

    def fold_into(self, run: Run) -> None:
        """Fold accumulator totals onto a run's persisted metrics, in place.

        The single home for the accumulator→metrics arithmetic (the other
        direction — child result→accumulator — is :func:`fold_spawn_fields`);
        ``RunLoop._finalize_run`` calls this at hop end so child spend lands
        on the parent's persisted ``Run.metrics``. No-op when nothing
        was spawned.
        """
        if self.spawn_count == 0:
            return
        m = run.metrics
        m.cost_usd += self.cost_usd
        m.input_tokens += self.input_tokens
        m.output_tokens += self.output_tokens
        m.turn_count += self.turns
        if self.tool_history:
            run.tool_history.extend(self.tool_history)
        logger.debug(
            "[spawn] folded %d sub-agent spawns: +$%.4f, +%d turns, +%d tools",
            self.spawn_count,
            self.cost_usd,
            self.turns,
            len(self.tool_history),
        )
