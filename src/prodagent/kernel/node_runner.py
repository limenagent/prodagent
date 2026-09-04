"""NodeRunner — one DAG node: resolve params, execute, classify the outcome.

A node is to PLAN_FIRST what a tool round is to REACTIVE, and it funnels
into the same throat: the identical dispatcher pipeline (approval gate,
hooks, breaker, spill truncation), so policy behaves the same in both
execution modes. What is plan-specific is the outcome algebra — a tool
result maps onto one of three node outcomes (success / failed / suspended)
— and the parking rule for the last one lives behind one lock so
concurrently-gathered nodes can't double-park a run.

Progress writes go through :class:`NodeRuntimeState`'s single-entry
transitions on the run — a node itself is frozen blueprint and never
touched here.
"""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, Any

from prodagent.base.errors import (
    BudgetExceeded,
    InfiniteLoopDetected,
    SuspendPendingApproval,
    ToolAbortError,
    ToolBlockedError,
)
from prodagent.kernel.bodies import ToolBody
from prodagent.kernel.body import (
    LLMInvoker,
    NodeContext,
    Outcome,
    SubagentInvoker,
    ToolExecutor,
    coerce_result,
)
from prodagent.kernel.bus import HookEvent
from prodagent.kernel.bus import fire as _fire
from prodagent.kernel.command import Command, Handoff, command_from_wire
from prodagent.kernel.interrupt import Interrupt
from prodagent.kernel.types import (
    Message,
    NodeStatus,
    RunState,
    ToolCall,
    ToolOutcome,
    ToolResult,
)

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator, Callable, Mapping

    from prodagent.kernel.bus import HookRegistry
    from prodagent.kernel.channels import WaveWrites
    from prodagent.kernel.event_log import PlanEventLog
    from prodagent.kernel.graph import Node, Plan
    from prodagent.kernel.node_state import NodeRuntimeState
    from prodagent.kernel.run import Run
    from prodagent.kernel.types import AgentEvent
    from prodagent.tooling.dispatcher import ToolDispatcher

logger = logging.getLogger(__name__)

__all__ = [
    "NodeRunner",
    "NodeSuccess",
    "NodeFailed",
    "NodeSuspended",
    "NodeOutcome",
]


def _extract_commands(raw: Any) -> tuple[Command, ...]:
    """Commands out of a body's return value: a Command directly, a list of
    them, or a plain dict marker (``{"goto": ...}`` — no framework types
    required of the fn author)."""
    if isinstance(raw, Command):
        return (raw,)
    if isinstance(raw, list) and raw and all(isinstance(c, Command) for c in raw):
        return tuple(raw)
    if isinstance(raw, dict):
        if (c := command_from_wire(raw)) is not None:
            return (c,)
        if isinstance(raw.get("commands"), list):
            out = [
                c for d in raw["commands"] if isinstance(d, dict) and (c := command_from_wire(d))
            ]
            return tuple(out)
    return ()


def _call_id(node_id: str, run_id: str) -> str:
    # Deterministic (not uuid): the call_id ties tool messages to nodes across
    # replays, and spill filenames derive from it — random ids would orphan
    # them on resume.
    return f"plan_{node_id}_{run_id}"


def _to_message_content(result: Any) -> str:
    if isinstance(result, str):
        return result
    return json.dumps(result, ensure_ascii=False, default=str)


def format_node_output(result: Any) -> str:
    if isinstance(result, dict) and "output" in result and "state" in result:
        inner = result["output"]
        if isinstance(inner, str) and inner:
            return inner
    if isinstance(result, str):
        return result
    return str(result)


def commit_transcript(node: Node, success: NodeSuccess, run: Run) -> None:
    """Commit a completed node's transcript fragment onto the run."""
    if success.tool_message is None:
        return
    run.messages.append(success.tool_message)
    run.last_action = f"{node.action}({list(node.params.keys())})"
    run.tool_history.append(success.call)


@dataclass(frozen=True, slots=True)
class NodeSuccess:
    node: Node
    result: ToolResult
    call: ToolCall
    tool_message: Message | None = None
    commands: tuple[Command, ...] = ()


@dataclass(frozen=True, slots=True)
class NodeFailed:
    node: Node
    error: BaseException
    call: ToolCall | None = None


@dataclass(frozen=True, slots=True)
class NodeSuspended:
    node: Node
    request_id: str | None
    tool: str
    call: ToolCall


NodeOutcome = NodeSuccess | NodeFailed | NodeSuspended


class NodeRunner:
    def __init__(
        self,
        log: PlanEventLog | None,
        *,
        hooks: HookRegistry | None = None,
        agent_name: str = "",
        dispatcher: ToolDispatcher | None = None,
        tools: ToolExecutor | None = None,
        llm: LLMInvoker | None = None,
        subagent: SubagentInvoker | None = None,
        wiring: Mapping[str, Any] | None = None,
        fns: Mapping[str, Callable[..., Any]] | None = None,
        wave_writes: WaveWrites | None = None,
        track_events: bool = True,
    ) -> None:
        self._log = log
        self._track_events = track_events
        self._hooks = hooks
        self._agent_name = agent_name
        self._dispatcher = dispatcher
        self._tools = tools
        self._llm = llm
        self._subagent = subagent
        self._wiring = dict(wiring) if wiring else {}
        self._wave_writes = wave_writes
        self._fns = dict(fns) if fns else {}
        self._commit_lock = asyncio.Lock()

    def _make_ctx(
        self, node: Node, run: Run, emit: Callable[[AgentEvent], None] | None = None
    ) -> NodeContext:
        """Per-execution wiring: the collaborator slots this runner was
        composed with, bound to this node's identity and this run."""
        return NodeContext(
            run_id=run.run_id,
            node_id=node.node_id,
            run=run,
            shared=run.shared,
            tools=self._tools,
            llm=self._llm,
            subagent=self._subagent,
            wiring=self._wiring,
            fns=self._fns,
            emit=emit,
        )

    async def run_one(
        self,
        node: Node,
        plan: Plan,
        run: Run,
    ) -> NodeOutcome:
        """Draining form: execute one node, discard any live events (none of
        the gathered kinds stream). See :meth:`stream_one` for the spine."""
        outcome: list[NodeOutcome] = []
        async for _ in self.stream_one(node, plan, run, outcome):
            pass
        return outcome[0]

    async def stream_one(
        self,
        node: Node,
        plan: Plan,
        run: Run,
        outcome: list[NodeOutcome],
    ) -> AsyncGenerator[AgentEvent, None]:
        """Execute one node and classify its outcome, forwarding the unit's
        live stream events (a run-driving body's rounds) as they happen.

        Never raises node failures (they return as :class:`NodeFailed` data);
        cancellation and *run-death* exceptions (budget exhausted, dead-loop)
        escape — those end the run, not the node. A SUSPENDED result parks
        the run before returning, which is what makes resume exact: the
        parked call is retried, not re-planned."""
        state = await self._start(node, plan, run)
        call = ToolCall(
            name=node.action,
            params=plan.resolve_params(node, run.node_states, run.shared),
            call_id=_call_id(node.node_id, run.run_id),
        )
        # Idempotency keys are a governed-tool concern: only a ToolBody has
        # registry metadata to demand them (a fn/llm name colliding with a
        # tool name must not borrow its contract).
        meta = (
            self._dispatcher.meta_for(call.name)
            if isinstance(node.body, ToolBody) and self._dispatcher is not None
            else None
        )
        if meta is not None and meta.enforced_idempotent:
            call.params.setdefault(
                "idempotency_key", f"{run.run_id}:{node.node_id}:a{state.attempts}"
            )
        buffered: list[AgentEvent] = []
        ctx = self._make_ctx(node, run, buffered.append)
        streaming = getattr(node.body, "run_stream", None)
        outcome_box: list[Outcome] = []
        try:
            if streaming is not None:
                # Streaming units run inline on this stack: suspending the
                # driver suspends the unit with it — an abandoned stream
                # freezes the work, never orphans it on the loop.
                async for event in streaming(call, ctx, outcome_box):
                    yield event
            else:
                outcome_box.append(await node.body.run(call, ctx))
                for event in buffered:
                    yield event
            unit_outcome = outcome_box[0]
            # The Outcome is the contract; the node-level algebra (commands,
            # ToolResult coercion) consumes its value. A declared channel
            # buffers to the wave barrier (column 7's fold discipline); an
            # undeclared key takes the legacy immediate path, where a second
            # writer without a reducer is the conflict it always was.
            raw = unit_outcome.value
            for key, value in unit_outcome.state_delta.items():
                if self._wave_writes is not None and self._wave_writes.is_declared(key):
                    self._wave_writes.buffer(key, value, node.node_id)
                    continue
                if key in run.shared:
                    raise ValueError(
                        f"node {node.node_id!r}: state_delta key {key!r} already "
                        "written and no reducer declared — two nodes merging one "
                        "key must say how"
                    )
                run.shared[key] = value
        except SuspendPendingApproval as exc:
            await self._park_suspended(
                node,
                ToolResult.suspended(
                    reason=str(exc),
                    tool=node.action,
                    approval_request_id=exc.request_id,
                ),
                call,
                plan,
                run,
            )
            outcome.append(
                NodeSuspended(
                    node=node,
                    request_id=exc.request_id,
                    tool=node.action,
                    call=call,
                )
            )
            return
        except (asyncio.CancelledError, BudgetExceeded, InfiniteLoopDetected):
            # Cancellation is the caller's; budget death and dead-loop
            # detection end the *run*, not the node — the scheduler settles.
            raise
        except Exception as exc:
            if getattr(node.body, "drives_run", False):
                # A run-driving body's crash is the run's crash:
                # settle-and-raise, never a replan candidate.
                raise
            outcome.append(NodeFailed(node=node, error=exc, call=call))
            return
        commands = _extract_commands(raw)
        # A command IS the node's outcome: extracting it leaves no data value
        # (a dict marker like {"goto": ...} carries nothing but the command).
        result = coerce_result(None if commands else raw, tool=node.action)

        if result.outcome is ToolOutcome.HANDOFF:
            # Control transfer lowers to a Handoff command: the scheduler
            # instantiates the peer as the chain's next node, and THIS node
            # completes normally with the handoff fact as its output.
            h = result.handoff or {}
            commands = (
                *commands,
                Handoff(peer=str(h.get("peer", "")), task=str(h.get("task", ""))),
            )

        if result.outcome is ToolOutcome.SUSPENDED:
            # Park before returning: resume retries this exact call.
            await self._park_suspended(node, result, call, plan, run)
            outcome.append(
                NodeSuspended(
                    node=node,
                    request_id=result.approval_request_id,
                    tool=node.action,
                    call=call,
                )
            )
            return

        if result.outcome in (ToolOutcome.ABORT, ToolOutcome.RETRY):
            # RED and YELLOW both become a plan-node failure here — the plan
            # has no per-node retry loop; replanning IS the recovery.
            error_msg = result.error.message if result.error is not None else ""
            if error_msg and result.error is not None and result.error.hint:
                error_msg = f"{error_msg} — hint: {result.error.hint}"
            outcome.append(
                NodeFailed(
                    node=node,
                    error=ToolAbortError(error_msg or "Tool returned red error"),
                    call=call,
                )
            )
            return

        if result.outcome is ToolOutcome.BLOCKED:
            reason = result.reason or "approval denied"
            outcome.append(
                NodeFailed(
                    node=node,
                    error=ToolBlockedError(f"HITL: tool '{node.action}' blocked — {reason}"),
                    call=call,
                )
            )
            return

        tool_message = await self._complete(node, result, call, plan, run)
        outcome.append(
            NodeSuccess(
                node=node,
                result=result,
                call=call,
                tool_message=tool_message,
                commands=commands,
            )
        )
        return

    async def _start(self, node: Node, plan: Plan, run: Run) -> NodeRuntimeState:
        """RUNNING is recorded in the event log before the tool fires — if the
        process dies mid-execution, restore sees RUNNING and resets the node
        to PENDING (redo), never silently skipping it."""
        state = run.node_state(node.node_id)
        state.mark_running()
        if self._log is not None and self._track_events:
            await self._log.record_node_started(plan, run, node.node_id)
        await _fire(
            self._hooks,
            HookEvent.NODE_STARTED,
            plan_id=run.run_id,
            node_id=node.node_id,
            action=node.action,
            run_id=run.run_id,
        )
        return state

    async def _complete(
        self,
        node: Node,
        result: ToolResult,
        call: ToolCall,
        plan: Plan,
        run: Run,
    ) -> Message | None:
        """Mark a node COMPLETED and return its transcript fragment."""
        async with self._commit_lock:
            state = run.node_state(node.node_id)
            if state.status is not NodeStatus.RUNNING:
                logger.info(
                    "[Plan] node=%s action=%s → abort completion (status=%s)",
                    node.node_id,
                    node.action,
                    state.status.value,
                )
                return None
            if getattr(node.body, "drives_run", False):
                # A run-driving body already wrote the run's transcript; the
                # node commits the run's final output unwrapped and no fragment.
                state.mark_completed(result.value)
                logger.info("[Plan] node=%s run-driver → COMPLETED", node.node_id)
                if self._log is not None and self._track_events:
                    await self._log.record_node_completed(plan, run, node.node_id, result.value)
                await _fire(
                    self._hooks,
                    HookEvent.NODE_COMPLETED,
                    plan_id=run.run_id,
                    node_id=node.node_id,
                    action=node.action,
                    run_id=run.run_id,
                )
                return None
            wire = result.to_wire()
            state.mark_completed(wire)
            logger.info("[Plan] node=%s action=%s → COMPLETED", node.node_id, node.action)
            if self._log is not None and self._track_events:
                await self._log.record_node_completed(plan, run, node.node_id, wire)
            await _fire(
                self._hooks,
                HookEvent.NODE_COMPLETED,
                plan_id=run.run_id,
                node_id=node.node_id,
                action=node.action,
                run_id=run.run_id,
            )
            if self._dispatcher is not None:
                # Same throat as REACTIVE: spill truncation and max_result_chars
                # apply to plan nodes too, not just loop batches.
                return self._dispatcher.build_tool_message(wire, call, run)
            return {
                "role": "tool",
                "tool_call_id": call.call_id,
                "content": _to_message_content(wire),
            }

    async def _park_suspended(
        self,
        node: Node,
        result: ToolResult,
        call: ToolCall,
        plan: Plan,
        run: Run,
    ) -> None:
        async with self._commit_lock:
            # Only the first suspension
            # parks; a run already parked elsewhere (a run-driver's dispatcher
            # did it) keeps that park — this layer refines it with the node
            # identity the dispatcher could not know. The NODE keeps no
            # suspended status: it stays RUNNING (mid-flight), which resume
            # reads as "unknown partial state, redo" — the redo retries the
            # staged call verbatim.
            if run.state is not RunState.SUSPENDED and not run.park(
                Interrupt.from_result(result, call)
            ):
                return
            if run.interrupt is not None and not run.interrupt.node_id:
                run.interrupt = replace(run.interrupt, node_id=node.node_id)
            if self._log is not None:
                # Park persists: a fresh plan snapshot under the interrupt —
                # the resumed process redoes this node and retries the
                # staged call.
                await self._log.save_snapshot(run, plan=plan)
            logger.info(
                "[Plan] run interrupted at %s (node=%s, request_id=%s)",
                node.action,
                node.node_id,
                result.approval_request_id or "(none)",
            )
