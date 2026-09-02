"""NodeRunner — one DAG node: resolve params, execute, classify the outcome.

A node is to PLAN_FIRST what a tool round is to REACTIVE, and it funnels
into the same throat: the identical dispatcher pipeline (approval gate,
hooks, breaker, spill truncation), so policy behaves the same in both
execution modes. What is plan-specific is the outcome algebra — a tool
result maps onto one of four node outcomes (success / failed / suspended /
handoff) — and the parking rules for the last two live behind one lock so
concurrently-gathered nodes can't double-park a run.

Progress writes go through :class:`NodeRuntimeState`'s single-entry
transitions on the run — a node itself is frozen blueprint and never
touched here.
"""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from prodagent.base.determinism import new_uuid4
from prodagent.base.errors import (
    BudgetExceeded,
    InfiniteLoopDetected,
    SuspendPendingApproval,
    ToolAbortError,
    ToolBlockedError,
)
from prodagent.hooks import fire as _fire
from prodagent.kernel.bodies.base import ReActBody, ToolBody
from prodagent.kernel.bus import HookEvent
from prodagent.kernel.state import AgentRun, PendingHandoff
from prodagent.kernel.types import (
    Message,
    NodeStatus,
    RunState,
    ToolCall,
    ToolOutcome,
    ToolResult,
)
from prodagent.plan.command import Command, command_from_wire
from prodagent.tooling.base import coerce_result

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator

    from prodagent.kernel.bodies.runner import BodyRunner
    from prodagent.kernel.bus import HookRegistry
    from prodagent.kernel.node_state import NodeRuntimeState
    from prodagent.kernel.types import AgentEvent
    from prodagent.plan.dag import Node, Plan
    from prodagent.plan.event_log import PlanEventLog
    from prodagent.tooling.dispatcher import ToolDispatcher

logger = logging.getLogger(__name__)

__all__ = [
    "NodeRunner",
    "NodeSuccess",
    "NodeFailed",
    "NodeSuspended",
    "NodeHandoff",
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


def commit_transcript(node: Node, success: NodeSuccess, run: AgentRun) -> None:
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


@dataclass(frozen=True, slots=True)
class NodeHandoff:
    node: Node
    handoff: PendingHandoff
    call: ToolCall


NodeOutcome = NodeSuccess | NodeFailed | NodeSuspended | NodeHandoff


class NodeRunner:
    def __init__(
        self,
        bodies: BodyRunner,
        log: PlanEventLog | None,
        *,
        hooks: HookRegistry | None = None,
        agent_name: str = "",
        dispatcher: ToolDispatcher | None = None,
    ) -> None:
        self._bodies = bodies
        self._log = log
        self._hooks = hooks
        self._agent_name = agent_name
        self._dispatcher = dispatcher
        self._commit_lock = asyncio.Lock()

    async def run_one(
        self,
        node: Node,
        plan: Plan,
        run: AgentRun,
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
        run: AgentRun,
        outcome: list[NodeOutcome],
    ) -> AsyncGenerator[AgentEvent, None]:
        """Execute one node and classify its outcome, forwarding the body's
        live stream events (a ReActBody's Turns) as they happen.

        Never raises node failures (they return as :class:`NodeFailed` data);
        cancellation and *run-death* exceptions (budget exhausted, dead-loop)
        escape — those end the run, not the node. SUSPENDED/HANDOFF park the
        run before returning, which is what makes resume exact: the parked
        call is retried, not re-planned."""
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
        if run.pending_handoff is not None:
            # A sibling in this batch already handed control away — this node
            # never fires, so report a no-op success with nothing to commit.
            outcome.append(
                NodeSuccess(
                    node=node,
                    result=ToolResult(ToolOutcome.OK, tool=node.action),
                    call=call,
                )
            )
            return
        raw_box: list[Any] = []
        try:
            async for event in self._bodies.run_stream(node.body, call, run, raw_box):
                yield event
        # extracted here so the except clauses below never see a Command
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
            if isinstance(node.body, ReActBody):
                # A react loop's crash is the run's crash (old REACTIVE
                # semantics): settle-and-raise, never a replan candidate.
                raise
            outcome.append(NodeFailed(node=node, error=exc, call=call))
            return
        commands = _extract_commands(raw_box[0])
        # A command IS the node's outcome: extracting it leaves no data value
        # (a dict marker like {"goto": ...} carries nothing but the command).
        result = coerce_result(None if commands else raw_box[0], tool=node.action)

        if result.outcome is ToolOutcome.HANDOFF:
            await self._park_handoff(node, result, call, plan, run)
            assert run.pending_handoff is not None  # parked above (or by a concurrent node)
            outcome.append(NodeHandoff(node=node, handoff=run.pending_handoff, call=call))
            return

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

    async def _start(self, node: Node, plan: Plan, run: AgentRun) -> NodeRuntimeState:
        """RUNNING is recorded in the event log before the tool fires — if the
        process dies mid-execution, restore sees RUNNING and resets the node
        to PENDING (redo), never silently skipping it."""
        state = run.node_state(node.node_id)
        state.mark_running()
        if self._log is not None:
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
        run: AgentRun,
    ) -> Message | None:
        """Mark a node COMPLETED and return its transcript fragment."""
        async with self._commit_lock:
            if run.pending_handoff is not None:
                return None
            state = run.node_state(node.node_id)
            if state.status is not NodeStatus.RUNNING:
                logger.info(
                    "[Plan] node=%s action=%s → abort completion (status=%s)",
                    node.node_id,
                    node.action,
                    state.status.value,
                )
                return None
            if isinstance(node.body, ReActBody):
                # The engine already wrote the run's transcript; the node
                # commits the run's final output unwrapped and no fragment.
                state.mark_completed(result.value)
                logger.info("[Plan] node=%s react → COMPLETED", node.node_id)
                if self._log is not None:
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
            if self._log is not None:
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

    async def _park_handoff(
        self,
        node: Node,
        result: ToolResult,
        call: ToolCall,
        plan: Plan,
        run: AgentRun,
    ) -> None:
        async with self._commit_lock:
            if run.pending_handoff is None:
                h = result.handoff or {}
                peer = h.get("peer", "")
                # First handoff wins across concurrently gathered nodes — the
                # park method owns that invariant (and finishes the run).
                if not run.park_handoff(
                    PendingHandoff(
                        peer_name=peer,
                        task=h.get("task", ""),
                        input_refs=dict(h.get("input_refs") or {}),
                        message_id=new_uuid4(),
                    )
                ):
                    return
            state = run.node_state(node.node_id)
            state.mark_completed(result.to_wire())
            if self._log is not None:
                await self._log.record_node_completed(plan, run, node.node_id, state.output_ref)
            peer_name = run.pending_handoff.peer_name if run.pending_handoff else "?"
            logger.info("[Plan] run handed off to peer=%s (node=%s)", peer_name, node.node_id)

    async def _park_suspended(
        self,
        node: Node,
        result: ToolResult,
        call: ToolCall,
        plan: Plan,
        run: AgentRun,
    ) -> None:
        async with self._commit_lock:
            # A handoff wins over a suspension; and only the first suspension
            # parks its pending call, so a resumed run retries the right tool.
            # A run already parked elsewhere (a react loop's dispatcher did it)
            # skips the park but still flips this node — resume must requeue it.
            if run.state is not RunState.SUSPENDED and not run.park_for_approval(
                call, result.approval_request_id or None
            ):
                return
            run.node_state(node.node_id).suspend()
            if self._log is not None:
                await self._log.record_node_suspended(plan, run, node.node_id)
            logger.info(
                "[Plan] run suspended pending approval: %s (node=%s, request_id=%s)",
                node.action,
                node.node_id,
                result.approval_request_id or "(none)",
            )
