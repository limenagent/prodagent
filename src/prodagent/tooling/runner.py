from __future__ import annotations

import asyncio
import json
import logging
from typing import TYPE_CHECKING, Any

from prodagent.cognition.context.tool_results import reduce_on_append
from prodagent.core.config import LoopConfig
from prodagent.core.error_reason import ErrorReason
from prodagent.core.events import (
    AgentEvent,
    ToolCallStartEvent,
    ToolResultEvent,
)
from prodagent.core.types import (
    SKILL_INJECTION_KEY,
    ErrorSeverity,
    Message,
    RunState,
    ToolCall,
    ToolError,
    ToolOutcome,
    ToolResult,
)

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from prodagent.cognition.context.spill import ToolResultSpillStore
    from prodagent.core.config import ContextConfig
    from prodagent.core.progress import ProgressMonitor
    from prodagent.core.state.run import AgentRun
    from prodagent.tooling.dispatcher import ToolDispatcher

logger = logging.getLogger(__name__)


class ToolRunner:
    def __init__(
        self,
        dispatcher: ToolDispatcher,
        *,
        loop_config: LoopConfig | None = None,
        context_config: ContextConfig | None = None,
        spill_store: ToolResultSpillStore | None = None,
        progress_monitor: ProgressMonitor | None = None,
    ) -> None:
        self._dispatcher = dispatcher
        self._loop_config = loop_config
        self._context_config = context_config
        self._spill_store = spill_store
        self._progress = progress_monitor

    async def run_batch(
        self,
        run: AgentRun,
        calls: list[ToolCall],
    ) -> AsyncIterator[AgentEvent]:
        readonly_concurrency = (
            self._loop_config.readonly_concurrency
            if self._loop_config
            else LoopConfig().readonly_concurrency
        )
        readonly_calls: list[tuple[int, ToolCall]] = []
        serial_calls: list[tuple[int, ToolCall]] = []
        deferred_injections: list[str] = []

        for i, call in enumerate(calls):
            if self._progress is not None:
                self._progress.check(run, new_call=call)

            meta = self._dispatcher.meta_for(call.name)
            if meta is not None and meta.enforced_idempotent:
                run.idempotency_seq += 1
                call.params.setdefault("idempotency_key", f"{run.run_id}:c{run.idempotency_seq}")

            run.last_action = f"{call.name}({list(call.params.keys())})"
            run.tool_history.append(call)
            yield ToolCallStartEvent(call=call, run_id=run.run_id)
            logger.debug("AgentLoop[%s] queued tool: %s", run.run_id, call.name)

            if self._dispatcher.is_readonly(call.name):
                readonly_calls.append((i, call))
            else:
                serial_calls.append((i, call))

        if readonly_calls:
            semaphore = asyncio.Semaphore(readonly_concurrency)

            async def _dispatch_with_cap(call: ToolCall) -> ToolResult:
                async with semaphore:
                    return await self._dispatcher.dispatch(call, run_id=run.run_id)

            raw = await asyncio.gather(
                *[_dispatch_with_cap(c) for _, c in readonly_calls],
                return_exceptions=True,
            )
            for (_, call), outcome in zip(readonly_calls, raw, strict=True):
                result = self._coerce_outcome(outcome, call, run)
                if self._emit_result(result, call, run, deferred_injections):
                    yield ToolResultEvent(name=call.name, result=result, run_id=run.run_id)
                    return
                yield ToolResultEvent(name=call.name, result=result, run_id=run.run_id)

        for _, call in serial_calls:
            result = await self._dispatcher.dispatch_with_retry(call, run)
            if self._emit_result(result, call, run, deferred_injections):
                yield ToolResultEvent(name=call.name, result=result, run_id=run.run_id)
                return
            yield ToolResultEvent(name=call.name, result=result, run_id=run.run_id)

        for injection in deferred_injections:
            run.messages.append(Message(role="user", content=injection))

    def _emit_result(
        self,
        result: ToolResult,
        call: ToolCall,
        run: AgentRun,
        deferred_injections: list[str],
    ) -> bool:
        if self._is_handoff(result, call, run):
            return True
        if self._is_suspended(result, call, run):
            return True
        wire = result.to_wire()
        injection = wire.pop(SKILL_INJECTION_KEY, None) if isinstance(wire, dict) else None
        run.messages.append(self._build_tool_message(wire, call, run))
        if injection:
            deferred_injections.append(injection)
        return False

    def _build_tool_message(self, wire: dict[str, Any], call: ToolCall, run: AgentRun) -> Message:
        if self._context_config is None:
            return Message(role="tool", tool_call_id=call.call_id, content=json.dumps(wire))
        meta = self._dispatcher.meta_for(call.name)
        max_result_chars = meta.max_result_chars if meta is not None else 100_000
        return reduce_on_append(
            wire, call, self._context_config, self._spill_store, max_result_chars=max_result_chars
        )

    @staticmethod
    def _is_suspended(result: ToolResult, call: ToolCall, run: AgentRun) -> bool:
        if result.outcome is ToolOutcome.SUSPENDED:
            run.state = RunState.SUSPENDED
            run.pending_tool_call = call
            run.pending_approval_id = result.approval_request_id or None
            run.tool_history = [c for c in run.tool_history if c is not call]
            return True
        return False

    @staticmethod
    def _is_handoff(result: ToolResult, call: ToolCall, run: AgentRun) -> bool:
        if result.outcome is not ToolOutcome.HANDOFF:
            return False
        from prodagent.core.state.run import PendingHandoff

        h = result.handoff or {}
        peer = h.get("peer", "")
        run.state = RunState.COMPLETED
        run.pending_handoff = PendingHandoff(
            peer_name=peer,
            task=h.get("task", ""),
            input_refs=dict(h.get("input_refs") or {}),
        )
        run.final_output = f"Handed off to {peer}" if peer else "Handed off"
        run.tool_history = [c for c in run.tool_history if c is not call]
        return True

    @staticmethod
    def _coerce_outcome(outcome: Any, call: ToolCall, run: AgentRun) -> ToolResult:
        if isinstance(outcome, BaseException):
            run.tool_failures += 1
            logger.error("Tool '%s' parallel error: %s", call.name, outcome)
            return ToolResult.from_error(
                ToolError.from_reason(
                    ErrorReason.UNKNOWN,
                    code="tool_parallel_error",
                    message=str(outcome),
                    severity=ErrorSeverity.YELLOW,
                ),
                tool=call.name,
            )
        if isinstance(outcome, ToolResult):
            return outcome
        return ToolResult.from_raw(outcome, tool=call.name)
