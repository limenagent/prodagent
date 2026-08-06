"""Single-step runner for PLAN_FIRST execution."""

from __future__ import annotations

import asyncio
import json
import logging
import time
from collections.abc import Callable, Coroutine
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from prodagent.core.exceptions import SuspendPendingApproval, ToolAbortError, ToolBlockedError
from prodagent.core.state.run import AgentRun, PendingHandoff
from prodagent.core.types import Message, RunState, StepStatus, ToolCall, ToolOutcome, ToolResult
from prodagent.hooks import fire as _fire
from prodagent.hooks.events import HookEvent

if TYPE_CHECKING:
    from prodagent.hooks.registry import HookRegistry
    from prodagent.runtime.plan.dag import Plan, PlanStep
    from prodagent.runtime.plan.event_log import PlanEventLog

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


ToolExecutor = Callable[[ToolCall], Coroutine[Any, Any, ToolResult]]


def _call_id(step_id: str, run_id: str) -> str:
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
    ) -> None:
        self._execute_tool = tool_executor
        self._log = log
        self._hooks = hooks
        self._agent_name = agent_name
        self._commit_lock = asyncio.Lock()

    async def run_one(
        self,
        step: PlanStep,
        plan: Plan,
        run: AgentRun,
    ) -> StepOutcome:
        await self._start(step, plan, run)
        call = ToolCall(
            name=step.action,
            params=plan.resolve_params(step),
            call_id=_call_id(step.step_id, run.run_id),
        )
        if run.pending_handoff is not None:
            return StepSuccess(
                step=step,
                result=ToolResult(ToolOutcome.OK, tool=step.action),
                call=call,
            )
        try:
            raw = await self._execute_tool(call)
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
        result = ToolResult.from_raw(raw, tool=step.action)

        if result.outcome is ToolOutcome.HANDOFF:
            await self._park_handoff(step, result, call, plan, run)
            assert run.pending_handoff is not None  # parked above (or by a concurrent step)
            return StepHandoff(step=step, handoff=run.pending_handoff, call=call)

        if result.outcome is ToolOutcome.SUSPENDED:
            await self._park_suspended(step, result, call, plan, run)
            return StepSuspended(
                step=step,
                request_id=result.approval_request_id,
                tool=step.action,
                call=call,
            )

        if result.outcome in (ToolOutcome.ABORT, ToolOutcome.RETRY):
            error_msg = result.error.message if result.error is not None else ""
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
            step.completed_at = time.monotonic()
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
            # First handoff wins across concurrently gathered steps.
            if run.pending_handoff is not None:
                return
            h = result.handoff or {}
            peer = h.get("peer", "")
            step.status = StepStatus.COMPLETED
            step.output_ref = result.to_wire()
            run.state = RunState.COMPLETED
            run.pending_handoff = PendingHandoff(
                peer_name=peer,
                task=h.get("task", ""),
                input_refs=dict(h.get("input_refs") or {}),
            )
            run.final_output = f"Handed off to {peer}" if peer else "Handed off"
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
            if run.pending_handoff is not None or run.state is RunState.SUSPENDED:
                return
            step.status = StepStatus.SUSPENDED
            run.state = RunState.SUSPENDED
            run.pending_approval_id = result.approval_request_id or None
            run.pending_tool_call = call
            await self._log.record_step_suspended(plan, run, step.step_id)
            logger.info(
                "[Plan] run suspended pending approval: %s (step=%s, request_id=%s)",
                step.action,
                step.step_id,
                result.approval_request_id or "(none)",
            )
