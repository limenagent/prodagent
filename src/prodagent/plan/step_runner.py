"""StepRunner — one DAG step: resolve params, execute, classify the outcome.

A step is to PLAN_FIRST what a tool round is to REACTIVE, and it funnels
into the same throat: the identical dispatcher pipeline (approval gate,
hooks, breaker, spill truncation), so policy behaves the same in both
execution modes. What is plan-specific is the outcome algebra — a tool
result maps onto one of four step outcomes (success / failed / suspended /
handoff), and the parking rules for the last two live behind one lock so
concurrently-gathered steps can't double-park a run.
"""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Protocol

from prodagent.base.determinism import new_uuid4, now_monotonic
from prodagent.base.errors import SuspendPendingApproval, ToolAbortError, ToolBlockedError
from prodagent.hooks import fire as _fire
from prodagent.kernel.bus import HookEvent
from prodagent.kernel.state import AgentRun, PendingHandoff
from prodagent.kernel.types import Message, StepStatus, ToolCall, ToolOutcome, ToolResult
from prodagent.tooling.base import coerce_result

if TYPE_CHECKING:
    from prodagent.kernel.bus import HookRegistry
    from prodagent.plan.dag import Plan, PlanStep
    from prodagent.plan.event_log import PlanEventLog
    from prodagent.tooling.dispatcher import ToolDispatcher

logger = logging.getLogger(__name__)

__all__ = [
    "StepRunner",
    "StepSuccess",
    "StepFailed",
    "StepSuspended",
    "StepHandoff",
    "StepOutcome",
    "ToolExecutor",
]


class ToolExecutor(Protocol):
    """Executes one tool call — the same shape as ``ToolDispatcher.dispatch``
    so hook payloads and ``inject_run_id`` tools work identically in both
    execution modes. ``run_id`` is required in practice; the default keeps
    hand-written executors in tests working."""

    async def __call__(self, call: ToolCall, *, run_id: str = "") -> ToolResult: ...


def _call_id(step_id: str, run_id: str) -> str:
    # Deterministic (not uuid): the call_id ties tool messages to steps across
    # replays, and spill filenames derive from it — random ids would orphan
    # them on resume.
    return f"plan_{step_id}_{run_id}"


def _to_message_content(result: Any) -> str:
    if isinstance(result, str):
        return result
    return json.dumps(result, ensure_ascii=False, default=str)


def format_step_output(result: Any) -> str:
    if isinstance(result, dict) and "output" in result and "state" in result:
        inner = result["output"]
        if isinstance(inner, str) and inner:
            return inner
    if isinstance(result, str):
        return result
    return str(result)


def commit_transcript(step: PlanStep, success: StepSuccess, run: AgentRun) -> None:
    """Commit a completed step's transcript fragment onto the run."""
    if success.tool_message is None:
        return
    run.messages.append(success.tool_message)
    run.last_action = f"{step.action}({list(step.params.keys())})"
    run.tool_history.append(success.call)


@dataclass(frozen=True, slots=True)
class StepSuccess:
    step: PlanStep
    result: ToolResult
    call: ToolCall
    tool_message: Message | None = None


@dataclass(frozen=True, slots=True)
class StepFailed:
    step: PlanStep
    error: BaseException
    call: ToolCall | None = None


@dataclass(frozen=True, slots=True)
class StepSuspended:
    step: PlanStep
    request_id: str | None
    tool: str
    call: ToolCall


@dataclass(frozen=True, slots=True)
class StepHandoff:
    step: PlanStep
    handoff: PendingHandoff
    call: ToolCall


StepOutcome = StepSuccess | StepFailed | StepSuspended | StepHandoff


class StepRunner:
    def __init__(
        self,
        tool_executor: ToolExecutor,
        log: PlanEventLog,
        *,
        hooks: HookRegistry | None = None,
        agent_name: str = "",
        dispatcher: ToolDispatcher | None = None,
    ) -> None:
        self._execute_tool = tool_executor
        self._log = log
        self._hooks = hooks
        self._agent_name = agent_name
        self._dispatcher = dispatcher
        self._commit_lock = asyncio.Lock()

    async def run_one(
        self,
        step: PlanStep,
        plan: Plan,
        run: AgentRun,
    ) -> StepOutcome:
        """Execute one step and classify its outcome — never raises step
        failures (they return as :class:`StepFailed` data); only cancellation
        escapes. SUSPENDED/HANDOFF park the run before returning, which is
        what makes resume exact: the parked call is retried, not re-planned."""
        await self._start(step, plan, run)
        call = ToolCall(
            name=step.action,
            params=plan.resolve_params(step),
            call_id=_call_id(step.step_id, run.run_id),
        )
        meta = self._dispatcher.meta_for(call.name) if self._dispatcher is not None else None
        if meta is not None and meta.enforced_idempotent:
            call.params.setdefault(
                "idempotency_key", f"{run.run_id}:{step.step_id}:a{step.attempts}"
            )
        if run.pending_handoff is not None:
            # A sibling in this batch already handed control away — this step
            # never fires, so report a no-op success with nothing to commit.
            return StepSuccess(
                step=step,
                result=ToolResult(ToolOutcome.OK, tool=step.action),
                call=call,
            )
        try:
            raw = await self._execute_tool(call, run_id=run.run_id)
        except SuspendPendingApproval as exc:
            await self._park_suspended(
                step,
                ToolResult.suspended(
                    reason=str(exc),
                    tool=step.action,
                    approval_request_id=exc.request_id,
                ),
                call,
                plan,
                run,
            )
            return StepSuspended(
                step=step,
                request_id=exc.request_id,
                tool=step.action,
                call=call,
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            return StepFailed(step=step, error=exc, call=call)
        result = coerce_result(raw, tool=step.action)

        if result.outcome is ToolOutcome.HANDOFF:
            await self._park_handoff(step, result, call, plan, run)
            assert run.pending_handoff is not None  # parked above (or by a concurrent step)
            return StepHandoff(step=step, handoff=run.pending_handoff, call=call)

        if result.outcome is ToolOutcome.SUSPENDED:
            # Park before returning: resume retries this exact call.
            await self._park_suspended(step, result, call, plan, run)
            return StepSuspended(
                step=step,
                request_id=result.approval_request_id,
                tool=step.action,
                call=call,
            )

        if result.outcome in (ToolOutcome.ABORT, ToolOutcome.RETRY):
            # RED and YELLOW both become a plan-step failure here — the plan
            # has no per-step retry loop; replanning IS the recovery.
            error_msg = result.error.message if result.error is not None else ""
            if error_msg and result.error is not None and result.error.hint:
                error_msg = f"{error_msg} — hint: {result.error.hint}"
            return StepFailed(
                step=step,
                error=ToolAbortError(error_msg or "Tool returned red error"),
                call=call,
            )

        if result.outcome is ToolOutcome.BLOCKED:
            reason = result.reason or "approval denied"
            return StepFailed(
                step=step,
                error=ToolBlockedError(f"HITL: tool '{step.action}' blocked — {reason}"),
                call=call,
            )

        tool_message = await self._complete(step, result, call, plan, run)
        return StepSuccess(step=step, result=result, call=call, tool_message=tool_message)

    async def _start(self, step: PlanStep, plan: Plan, run: AgentRun) -> None:
        """RUNNING is recorded in the event log before the tool fires — if the
        process dies mid-execution, restore sees RUNNING and resets the step
        to PENDING (redo), never silently skipping it."""
        step.status = StepStatus.RUNNING
        step.attempts += 1
        await self._log.record_step_started(plan, run, step.step_id)
        await _fire(
            self._hooks,
            HookEvent.STEP_STARTED,
            plan_id=run.run_id,
            step_id=step.step_id,
            action=step.action,
            run_id=run.run_id,
        )

    async def _complete(
        self,
        step: PlanStep,
        result: ToolResult,
        call: ToolCall,
        plan: Plan,
        run: AgentRun,
    ) -> Message | None:
        """Mark a step COMPLETED and return its transcript fragment."""
        async with self._commit_lock:
            if run.pending_handoff is not None:
                return None
            if step.status is not StepStatus.RUNNING:
                logger.info(
                    "[Plan] step=%s action=%s → abort completion (status=%s)",
                    step.step_id,
                    step.action,
                    step.status.value,
                )
                return None
            wire = result.to_wire()
            step.status = StepStatus.COMPLETED
            step.output_ref = wire
            step.completed_at = now_monotonic()
            logger.info("[Plan] step=%s action=%s → COMPLETED", step.step_id, step.action)
            await self._log.record_step_completed(plan, run, step.step_id, wire)
            await _fire(
                self._hooks,
                HookEvent.STEP_COMPLETED,
                plan_id=run.run_id,
                step_id=step.step_id,
                action=step.action,
                run_id=run.run_id,
            )
            if self._dispatcher is not None:
                # Same throat as REACTIVE: spill truncation and max_result_chars
                # apply to plan steps too, not just loop batches.
                return self._dispatcher.build_tool_message(wire, call, run)
            return {
                "role": "tool",
                "tool_call_id": call.call_id,
                "content": _to_message_content(wire),
            }

    async def _park_handoff(
        self,
        step: PlanStep,
        result: ToolResult,
        call: ToolCall,
        plan: Plan,
        run: AgentRun,
    ) -> None:
        async with self._commit_lock:
            h = result.handoff or {}
            peer = h.get("peer", "")
            # First handoff wins across concurrently gathered steps — the
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
            step.status = StepStatus.COMPLETED
            step.output_ref = result.to_wire()
            await self._log.record_step_completed(plan, run, step.step_id, step.output_ref)
            logger.info("[Plan] run handed off to peer=%s (step=%s)", peer, step.step_id)

    async def _park_suspended(
        self,
        step: PlanStep,
        result: ToolResult,
        call: ToolCall,
        plan: Plan,
        run: AgentRun,
    ) -> None:
        async with self._commit_lock:
            # A handoff wins over a suspension; and only the first suspension
            # parks its pending call, so a resumed run retries the right tool.
            if not run.park_for_approval(call, result.approval_request_id or None):
                return
            step.status = StepStatus.SUSPENDED
            await self._log.record_step_suspended(plan, run, step.step_id)
            logger.info(
                "[Plan] run suspended pending approval: %s (step=%s, request_id=%s)",
                step.action,
                step.step_id,
                result.approval_request_id or "(none)",
            )
