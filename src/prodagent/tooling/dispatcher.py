from __future__ import annotations

import asyncio
import json
import logging
import time
from contextlib import asynccontextmanager
from typing import TYPE_CHECKING, Any

from prodagent.base.config import ContextConfig, LoopConfig
from prodagent.base.errors import SECURITY_VETO_EXCEPTIONS, ErrorLayer, ErrorReason, classify_error
from prodagent.base.retry import Backoff, RetryPolicy
from prodagent.kernel.bus import Gate, HookEvent
from prodagent.kernel.types import (
    GET_SKILL_TOOL_NAME,
    SKILL_INJECTION_KEY,
    ErrorSeverity,
    Message,
    SideEffectLevel,
    ToolCall,
    ToolCallStartEvent,
    ToolError,
    ToolMeta,
    ToolOutcome,
    ToolResult,
    ToolResultEvent,
)
from prodagent.tooling.base import coerce_result
from prodagent.tooling.skill_resolver import SkillResolver

TRANSIENT_EXC: tuple[type[BaseException], ...] = (
    ConnectionError,
    OSError,
)


def _tool_failure(
    exc: BaseException, call: ToolCall, *, code: str, message: str, hint: str = ""
) -> ToolResult:
    classified = classify_error(exc, layer=ErrorLayer.TOOL)
    reason = ToolError.from_reason(classified.reason, code=code, message=message, hint=hint)
    return ToolResult.from_error(reason, tool=call.name)


if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Awaitable, Callable

    from prodagent.cognition.context.spill import ToolResultSpillStore
    from prodagent.kernel.bus import HookRegistry
    from prodagent.kernel.progress import ProgressMonitor
    from prodagent.kernel.state import AgentRun
    from prodagent.kernel.types import AgentEvent
    from prodagent.skills.registry import SkillRegistry
    from prodagent.tooling.base import FunctionTool
    from prodagent.tooling.registry import ToolRegistry

logger = logging.getLogger(__name__)

_DEFAULT_TOOL_TIMEOUT_S = 3.0


def _default_tool_retry_policy() -> RetryPolicy:
    # max_attempts=1: by default the executor does NOT retry — a YELLOW error
    # goes back to the model as structured feedback, and the model retries
    # with awareness (different params, different tool, or backing off). A
    # blind executor loop would hammer a failing dependency the model might
    # know how to route around.
    return RetryPolicy(
        max_attempts=1,
        base_delay=1.0,
        max_delay=5.0,
        backoff=Backoff.FIXED,
    )


class ToolDispatcher:
    """Executes tool-call batches: readonly calls in parallel, writes serial.

    Every result passes the same pipeline — probe (circuit breaker) → approval
    gate → pre-hooks → invoke (deadline-bounded) → post-hooks — so policy
    attaches once, not per call site.
    """

    def __init__(
        self,
        tool_map: dict[str, FunctionTool],
        *,
        tool_registry: ToolRegistry | None = None,
        hooks: HookRegistry | None = None,
        skills: SkillRegistry | None = None,
        agent_id: str = "",
        agent_name: str = "",
        retry_policy: RetryPolicy | None = None,
        pending_approval_id: str | None = None,
        skill_resolver: SkillResolver | None = None,
        loop_config: LoopConfig | None = None,
        context_config: ContextConfig | None = None,
        spill_store: ToolResultSpillStore | None = None,
        progress_monitor: ProgressMonitor | None = None,
    ) -> None:
        self._tool_map = tool_map
        self._loop_config = loop_config
        self._context_config = context_config
        self._spill_store = spill_store
        self._progress = progress_monitor
        self._tool_registry = tool_registry
        self._hooks = hooks
        self._agent_id = agent_id
        self._agent_name = agent_name
        self._retry_policy = retry_policy or _default_tool_retry_policy()
        self._skill_resolver = skill_resolver or SkillResolver(
            skills=skills,
            hooks=hooks,
            agent_id=agent_id or agent_name,
            pending_approval_id=pending_approval_id,
        )

    # -- Batch execution (readonly parallel / write serial) -------------------

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
        emitted: set[str] = set()

        for i, call in enumerate(calls):
            if self._progress is not None:
                self._progress.check(run, new_call=call)

            meta = self.meta_for(call.name)
            if meta is not None and meta.enforced_idempotent:
                # Crash-replay may re-fire this call; the host-minted key lets
                # the tool dedupe the second execution — at-most-once is the
                # tool's promise, the key is what makes it keepable.
                run.idempotency_seq += 1
                call.params.setdefault("idempotency_key", f"{run.run_id}:c{run.idempotency_seq}")

            run.last_action = f"{call.name}({list(call.params.keys())})"
            run.tool_history.append(call)
            yield ToolCallStartEvent(call=call, run_id=run.run_id)
            logger.debug("AgentLoop[%s] queued tool: %s", run.run_id, call.name)

            # Reads commute, writes don't: readonly calls may race under a
            # concurrency cap, while side-effecting calls run one at a time so
            # their observable order matches the order the model asked for.
            if self.is_readonly(call.name):
                readonly_calls.append((i, call))
            else:
                serial_calls.append((i, call))

        if readonly_calls:
            semaphore = asyncio.Semaphore(readonly_concurrency)

            async def _dispatch_with_cap(call: ToolCall) -> ToolResult:
                async with semaphore:
                    return await self.dispatch(call, run_id=run.run_id)

            raw = await asyncio.gather(
                *[_dispatch_with_cap(c) for _, c in readonly_calls],
                return_exceptions=True,
            )
            for (_, call), outcome in zip(readonly_calls, raw, strict=True):
                result = self._coerce_outcome(outcome, call, run)
                if self._emit_result(result, call, run, deferred_injections, emitted):
                    yield ToolResultEvent(name=call.name, result=result, run_id=run.run_id)
                    self._balance_batch(run, calls, emitted, keep=call)
                    self._flush_injections(run, deferred_injections)
                    return
                yield ToolResultEvent(name=call.name, result=result, run_id=run.run_id)

        for _, call in serial_calls:
            result = await self.dispatch_with_retry(call, run)
            if self._emit_result(result, call, run, deferred_injections, emitted):
                yield ToolResultEvent(name=call.name, result=result, run_id=run.run_id)
                self._balance_batch(run, calls, emitted, keep=call)
                self._flush_injections(run, deferred_injections)
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
        emitted: set[str] | None = None,
    ) -> bool:
        if self._is_handoff(result, call, run):
            return True
        if self._is_suspended(result, call, run):
            return True
        wire = result.to_wire()
        injection = wire.pop(SKILL_INJECTION_KEY, None) if isinstance(wire, dict) else None
        run.messages.append(self.build_tool_message(wire, call, run))
        if emitted is not None:
            emitted.add(call.call_id)
        if injection:
            deferred_injections.append(injection)
        return False

    @staticmethod
    def _balance_batch(
        run: AgentRun, calls: list[ToolCall], emitted: set[str], *, keep: ToolCall
    ) -> None:
        """Keep the transcript wire-valid when a batch ends early (suspend/handoff).

        The assistant message already carries all N tool_calls, but only the
        emitted ones got results. Providers reject a request whose tool_use
        blocks lack tool_results, so every never-dispatched sibling gets an
        explicit skip marker; the early-terminating call itself (``keep``) is
        excluded — a suspended call is replayed on resume and a handoff call
        is answered by the handoff path. Skipped calls also leave
        ``tool_history``: history records what actually ran."""
        for call in calls:
            if call is keep or call.call_id in emitted:
                continue
            run.messages.append(
                Message(
                    role="tool",
                    tool_call_id=call.call_id,
                    content=f"skipped: run ended before '{call.name}' was dispatched",
                )
            )
            run.tool_history = [c for c in run.tool_history if c is not call]

    @staticmethod
    def _flush_injections(run: AgentRun, deferred_injections: list[str]) -> None:
        """Don't drop already-collected skill injections when a batch ends early."""
        for injection in deferred_injections:
            run.messages.append(Message(role="user", content=injection))

    def build_tool_message(self, wire: dict[str, Any], call: ToolCall, run: AgentRun) -> Message:
        """The one way a tool result becomes a transcript message — shared by
        both execution modes so spill truncation and ``max_result_chars``
        behave identically whether the batch came from a REACTIVE turn or a
        PLAN_FIRST step."""
        if self._context_config is None:
            return Message(role="tool", tool_call_id=call.call_id, content=json.dumps(wire))
        from prodagent.cognition.context.tool_results import reduce_on_append

        meta = self.meta_for(call.name)
        max_result_chars = meta.max_result_chars if meta is not None else 100_000
        return reduce_on_append(
            wire, call, self._context_config, self._spill_store, max_result_chars=max_result_chars
        )

    @staticmethod
    def _is_suspended(result: ToolResult, call: ToolCall, run: AgentRun) -> bool:
        if result.outcome is ToolOutcome.SUSPENDED:
            # Batch discipline guarantees a single park here (suspension stops
            # the batch), so the bool is ignored — the method is the invariant.
            run.park_for_approval(call, result.approval_request_id or None)
            run.tool_history = [c for c in run.tool_history if c is not call]
            return True
        return False

    @staticmethod
    def _is_handoff(result: ToolResult, call: ToolCall, run: AgentRun) -> bool:
        if result.outcome is not ToolOutcome.HANDOFF:
            return False
        import uuid

        from prodagent.kernel.state import PendingHandoff

        h = result.handoff or {}
        run.park_handoff(
            PendingHandoff(
                peer_name=h.get("peer", ""),
                task=h.get("task", ""),
                input_refs=dict(h.get("input_refs") or {}),
                message_id=str(uuid.uuid4()),
            )
        )
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
        return coerce_result(outcome, tool=call.name)

    def configure_batch(
        self,
        *,
        loop_config: LoopConfig | None = None,
        context_config: ContextConfig | None = None,
        spill_store: ToolResultSpillStore | None = None,
        progress_monitor: ProgressMonitor | None = None,
    ) -> None:
        """Attach the batch-execution context (called by the executor build)."""
        self._loop_config = loop_config
        self._context_config = context_config
        self._spill_store = spill_store
        self._progress = progress_monitor

    # -- SkillResolver passthroughs (kept for executor compatibility) ---------

    def set_pending_approval_id(self, approval_id: str | None) -> None:
        """Set the approval id to replay on the next dispatch (HITL resume)."""
        self._skill_resolver.set_pending_approval_id(approval_id)

    def invoked_skills(self) -> dict[str, str]:
        return self._skill_resolver.invoked_skills()

    # -- introspection --------------------------------------------------------

    def is_readonly(self, name: str) -> bool:
        t = self._tool_map.get(name)
        return bool(t and getattr(t.meta, "is_readonly", False))

    def meta_for(self, name: str) -> ToolMeta | None:
        t = self._tool_map.get(name)
        return t.meta if t is not None else None

    # -- dispatch -------------------------------------------------------------

    async def dispatch(self, call: ToolCall, *, run_id: str = "") -> ToolResult:
        """Dispatch ``call`` through probe → approval gate → hooks → invoke → hooks.

        ``get_skill`` shares this pipeline but carries no ``ToolMeta``, so the
        approval gate (gated on ``meta``) no-ops for it.
        """
        if call.name == GET_SKILL_TOOL_NAME:
            meta = None

            async def invoke() -> tuple[ToolResult, float]:
                return await self._invoke_skill(call, run_id)
        else:
            fn_tool = self._tool_map.get(call.name)
            if fn_tool is None:
                return ToolResult.from_error(
                    ToolError.from_reason(
                        ErrorReason.TOOL_NOT_AVAILABLE,
                        code="tool_not_available",
                        message=(
                            f"Tool {call.name!r} is not available to agent {self._agent_name!r}. "
                            f"Available: {sorted(self._tool_map)}"
                        ),
                        hint="Call get_skill to load a skill that provides this tool.",
                    ),
                    tool=call.name,
                )
            meta = fn_tool.meta

            async def invoke() -> tuple[ToolResult, float]:
                return await self._invoke(call, fn_tool, meta, run_id)

        async with self._probe_slot(call.name) as acquired:
            if not acquired:
                return ToolResult.from_error(
                    ToolError.from_reason(
                        ErrorReason.OVERLOADED,
                        code="tool_circuit_open",
                        message=(
                            f"Tool {call.name!r} is in circuit-breaker OPEN state "
                            "(too many recent failures). Retry later."
                        ),
                        hint="Wait for the breaker to transition to HALF_OPEN and retry.",
                    ),
                    tool=call.name,
                )
            return await self._run_pipeline(call, meta, invoke, run_id)

    @asynccontextmanager
    async def _probe_slot(self, name: str) -> AsyncIterator[bool]:
        acquired = (
            await self._tool_registry.try_acquire_probe(name)
            if self._tool_registry is not None
            else True
        )
        try:
            yield acquired
        finally:
            if self._tool_registry is not None:
                await self._tool_registry.release_probe(name)

    async def _run_pipeline(
        self,
        call: ToolCall,
        meta: ToolMeta | None,
        invoke: Callable[[], Awaitable[tuple[ToolResult, float]]],
        run_id: str,
    ) -> ToolResult:
        if meta is not None and meta.side_effect_level is SideEffectLevel.HIGH:
            blocked = await self._skill_resolver.gate_approval(call, meta)
            if blocked is not None:
                return blocked

        if (blocked := await self._run_pre_hooks(call, meta, run_id)) is not None:
            return blocked

        try:
            result, elapsed_ms = await invoke()
        except SECURITY_VETO_EXCEPTIONS as exc:
            return ToolResult.blocked_by(str(exc), tool=call.name)

        if (blocked := await self._run_post_hooks(call, result, elapsed_ms, run_id)) is not None:
            return blocked
        return result

    async def dispatch_with_retry(self, call: ToolCall, run: AgentRun) -> ToolResult:
        policy = self._retry_policy
        max_retries = policy.max_attempts - 1
        run.reset_retry(call.name)
        for _attempt in range(policy.max_attempts):
            result = await self.dispatch(call, run_id=run.run_id)

            if result.outcome in (
                ToolOutcome.OK,
                ToolOutcome.BLOCKED,
                ToolOutcome.SUSPENDED,
                ToolOutcome.HANDOFF,
            ):
                # HANDOFF is terminal too — retrying it would re-fire a
                # handoff that already succeeded.
                return result

            assert result.error is not None

            if result.outcome is ToolOutcome.ABORT:
                logger.warning(
                    "Tool '%s' RED — permanent failure: %s", call.name, result.error.message
                )
                return result

            if result.error.reason is ErrorReason.RESOURCE_BUSY:
                logger.info(
                    "Tool '%s' RESOURCE_BUSY — deferring to the LLM (no executor retry): %s",
                    call.name,
                    result.error.message,
                )
                return result

            retry_count = run.retry_count(call.name)
            if retry_count >= max_retries:
                logger.warning("Tool '%s' YELLOW — retries exhausted", call.name)
                return result

            run.increment_retry(call.name)
            delay = policy.delay(retry_count + 1)
            logger.info(
                "Tool '%s' YELLOW — retry %d/%d in %.1fs: %s",
                call.name,
                retry_count + 1,
                max_retries,
                delay,
                result.error.message,
            )
            await asyncio.sleep(delay)

        raise RuntimeError(
            "dispatch_with_retry loop exhausted without returning"
        )  # pragma: no cover

    async def _run_pre_hooks(
        self, call: ToolCall, meta: ToolMeta | None, run_id: str
    ) -> ToolResult | None:
        if not self._hooks:
            return None
        payload = dict(
            call_id=call.call_id,
            name=call.name,
            params=call.params,
            side_effect_level=meta.side_effect_level.value if meta else "",
            readonly=meta.is_readonly if meta else True,
            approval_required=(meta.side_effect_level is SideEffectLevel.HIGH if meta else False),
            run_id=run_id,
        )
        await self._hooks.fire(HookEvent.TOOL_CALL, **payload)
        try:
            result = await self._hooks.check_blocking(Gate.TOOL_CALL, **payload)
        except SECURITY_VETO_EXCEPTIONS as exc:
            return ToolResult.blocked_by(str(exc), tool=call.name)
        if result.blocked:
            return ToolResult.blocked_by(result.reason or "blocked by pre-hook", tool=call.name)
        return None

    async def _invoke(
        self, call: ToolCall, fn_tool: FunctionTool, meta: ToolMeta | None, run_id: str
    ) -> tuple[ToolResult, float]:
        start = time.time()
        timeout = meta.timeout_seconds if meta is not None else _DEFAULT_TOOL_TIMEOUT_S

        async def _raw(c: ToolCall) -> ToolResult:
            return await asyncio.wait_for(fn_tool(**c.params, run_id=run_id), timeout=timeout)

        try:
            result = await _raw(call)

        except SECURITY_VETO_EXCEPTIONS:
            raise

        except TimeoutError as exc:
            timeout_s = meta.timeout_seconds if meta is not None else _DEFAULT_TOOL_TIMEOUT_S
            logger.warning("Tool %r exceeded its timeout (%s)", call.name, exc or "no message")
            result = await self._fail_tool(
                exc,
                call,
                code="tool_timeout",
                message=f"Tool '{call.name}' timed out after {timeout_s}s",
                hint="Retry, or increase the tool's timeout if the call is legitimately slow.",
            )

        except TRANSIENT_EXC as exc:
            logger.warning("Tool %r hit transient error: %s", call.name, exc)
            result = await self._fail_tool(
                exc,
                call,
                code="tool_transient_error",
                message=f"Tool '{call.name}' failed transiently: {exc}",
                hint="Retry with backoff.",
            )

        except Exception as exc:  # noqa: BLE001 — classify+record, then return
            logger.error("Tool %r failed: %s", call.name, exc)
            result = await self._fail_tool(exc, call, code="tool_execution_error", message=str(exc))

        else:
            await self._record_breaker(call.name, failure=False)

        return result, (time.time() - start) * 1000

    async def _invoke_skill(self, call: ToolCall, run_id: str) -> tuple[ToolResult, float]:
        start = time.time()
        try:
            result = await asyncio.wait_for(
                self._skill_resolver.resolve(call, run_id), timeout=_DEFAULT_TOOL_TIMEOUT_S
            )
        except TimeoutError as exc:
            logger.warning("get_skill exceeded its timeout: %s", exc or "no message")
            result = await self._fail_tool(
                exc,
                call,
                code="tool_timeout",
                message=f"Tool '{call.name}' timed out after {_DEFAULT_TOOL_TIMEOUT_S}s",
                hint="Skill loads should be fast; check the skill registry.",
            )
        except Exception as exc:  # noqa: BLE001
            logger.error("get_skill failed: %s", exc)
            result = await self._fail_tool(exc, call, code="tool_execution_error", message=str(exc))
        else:
            await self._record_breaker(call.name, failure=False)
        return result, (time.time() - start) * 1000

    async def _fail_tool(
        self, exc: BaseException, call: ToolCall, *, code: str, message: str, hint: str = ""
    ) -> ToolResult:
        """Classify ``exc`` into a ToolResult and record a breaker failure."""
        result = _tool_failure(exc, call, code=code, message=message, hint=hint)
        await self._record_breaker(call.name, failure=True)
        return result

    async def _record_breaker(self, name: str, *, failure: bool) -> None:
        if self._tool_registry is None:
            return
        if failure:
            await self._tool_registry.record_failure(name)
        else:
            await self._tool_registry.record_success(name)

    async def _run_post_hooks(
        self, call: ToolCall, result: ToolResult, elapsed_ms: float, run_id: str
    ) -> ToolResult | None:
        if not self._hooks:
            return None
        payload = dict(
            call_id=call.call_id,
            name=call.name,
            result=result.to_wire(),
            elapsed_ms=elapsed_ms,
            run_id=run_id,
        )
        await self._hooks.fire(HookEvent.TOOL_RESULT, **payload)
        try:
            check = await self._hooks.check_blocking(Gate.TOOL_RESULT, **payload)
        except SECURITY_VETO_EXCEPTIONS as exc:
            return ToolResult.blocked_by(str(exc), tool=call.name)
        if check.blocked:
            return ToolResult.blocked_by(check.reason or "blocked by post-hook", tool=call.name)
        return None
