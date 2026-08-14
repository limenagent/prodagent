from __future__ import annotations

import asyncio
import logging
import time
from contextlib import asynccontextmanager
from typing import TYPE_CHECKING

from prodagent.core.error_classifier import classify_error
from prodagent.core.error_reason import ErrorLayer, ErrorReason
from prodagent.core.exceptions import SECURITY_VETO_EXCEPTIONS
from prodagent.core.types import (
    GET_SKILL_TOOL_NAME,
    SideEffectLevel,
    ToolCall,
    ToolError,
    ToolMeta,
    ToolOutcome,
    ToolResult,
)
from prodagent.hooks.checkpoint import CheckPoint
from prodagent.hooks.events import HookEvent
from prodagent.resilience.reliability.retry import Backoff, RetryPolicy
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

    from prodagent.core.state.run import AgentRun
    from prodagent.evaluation.skills.registry import SkillRegistry
    from prodagent.hooks.registry import HookRegistry
    from prodagent.tooling.base import FunctionTool
    from prodagent.tooling.registry import ToolRegistry

logger = logging.getLogger(__name__)

_DEFAULT_TOOL_TIMEOUT_S = 3.0


def _default_tool_retry_policy() -> RetryPolicy:
    return RetryPolicy(
        max_attempts=4,  # 1 initial + 3 retries
        base_delay=1.0,
        max_delay=5.0,
        backoff=Backoff.FIXED,
    )


class ToolDispatcher:
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
    ) -> None:
        self._tool_map = tool_map
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

            if result.outcome in (ToolOutcome.OK, ToolOutcome.BLOCKED, ToolOutcome.SUSPENDED):
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
            result = await self._hooks.check_blocking(CheckPoint.TOOL_CALL, **payload)
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
            check = await self._hooks.check_blocking(CheckPoint.TOOL_RESULT, **payload)
        except SECURITY_VETO_EXCEPTIONS as exc:
            return ToolResult.blocked_by(str(exc), tool=call.name)
        if check.blocked:
            return ToolResult.blocked_by(check.reason or "blocked by post-hook", tool=call.name)
        return None
