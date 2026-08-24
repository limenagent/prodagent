"""Step — the atom of agency: one model call plus at most one tool round.

A REACTIVE loop is nothing but a policy for iterating Steps (when to stop,
what to resume, how to settle). Everything the Step needs from the world
arrives through collaborators — ``llm``, ``runner``, ``assembler``, ``bus`` —
so this module imports no capability package. Budget enforcement is injected
as a callable so the spawn-aware check stays outside the kernel.
"""

from __future__ import annotations

import asyncio
import dataclasses
import json
import logging
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

from prodagent.core.exceptions import BudgetExceeded
from prodagent.kernel.budget import HardBudget
from prodagent.kernel.bus import HookEvent, HookRegistry
from prodagent.kernel.events import ThinkTokenEvent
from prodagent.kernel.state import AgentRun
from prodagent.kernel.types import LLMResponse, Message, MessageList, RunState, StopReason

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator, Awaitable, Callable

    from prodagent.kernel.events import AgentEvent
    from prodagent.kernel.types import ToolCall
    from prodagent.ports.llm import LLMClient, LLMConfig

logger = logging.getLogger(__name__)

__all__ = ["Step", "ContextAssembler", "ToolRunner", "ProgressGuard"]


@runtime_checkable
class ContextAssembler(Protocol):
    """Prepares what the model sees this turn: ``(system, messages)``."""

    def __call__(self, run: AgentRun) -> Awaitable[tuple[str, MessageList]]: ...


@runtime_checkable
class ToolRunner(Protocol):
    """Executes a batch of tool calls against a run, yielding events."""

    def run_batch(
        self, run: AgentRun, calls: list[ToolCall]
    ) -> AsyncGenerator[AgentEvent, None]: ...


@runtime_checkable
class ProgressGuard(Protocol):
    """Dead-loop detection over the run's fingerprint window."""

    def check(self, run: AgentRun) -> None: ...


class Step:
    """Assemble context → call the model → account → act, once."""

    def __init__(
        self,
        llm: LLMClient,
        runner: ToolRunner,
        *,
        budget: HardBudget,
        guard: ProgressGuard | None = None,
        bus: HookRegistry | None = None,
        assembler: ContextAssembler | None = None,
        budget_check: Callable[[AgentRun], None] | None = None,
        llm_config: LLMConfig | None = None,
        cache_boundary: Callable[[], int | None] | None = None,
        phase: str = "reactive",
    ) -> None:
        self._llm = llm
        self._runner = runner
        self._budget = budget
        self._guard = guard
        self._bus = bus
        self._assembler = assembler
        self._budget_check = budget_check
        self._llm_config = llm_config
        self._cache_boundary = cache_boundary
        self._phase = phase

    def _check_budget(self, run: AgentRun) -> None:
        if self._budget_check is not None:
            self._budget_check(run)

    async def run(
        self,
        run: AgentRun,
        *,
        system: str,
        tools: list[dict[str, Any]] | None,
    ) -> AsyncGenerator[AgentEvent, None]:
        response, token_events = await self._think(run, system=system, tools=tools)
        for evt in token_events:
            yield evt
        if self._end_turn(run, response):
            return
        self._check_budget(run)
        async for event in self._runner.run_batch(run, response.tool_calls):
            yield event
        self._check_budget(run)

    async def _think(
        self,
        run: AgentRun,
        *,
        system: str,
        tools: list[dict[str, Any]] | None,
    ) -> tuple[LLMResponse, list[ThinkTokenEvent]]:
        system, messages = await self._prepare(run, system=system)
        response, token_events = await self._call_llm(run, system, messages, tools)
        await self._account(run, response)
        return response, token_events

    async def _prepare(self, run: AgentRun, *, system: str) -> tuple[str, MessageList]:
        self._check_budget(run)
        if self._guard is not None:
            self._guard.check(run)

        await _fire(
            self._bus,
            HookEvent.TURN_START,
            turn=run.turn_count + 1,
            max_turns=self._budget.max_turns,
            run_id=run.run_id,
        )

        if self._assembler is not None:
            system, messages = await self._assembler(run)
        else:
            messages = run.messages

        await _fire(
            self._bus,
            HookEvent.LLM_REQUEST,
            system=system[:200],
            system_len=len(system),
            messages=messages,
            msg_count=len(messages),
            phase=self._phase,
            run_id=run.run_id,
        )
        return system, messages

    async def _call_llm(
        self,
        run: AgentRun,
        system: str,
        messages: list[Message],
        tools: list[dict[str, Any]] | None,
    ) -> tuple[LLMResponse, list[ThinkTokenEvent]]:
        token_events: list[ThinkTokenEvent] = []

        async def _on_chunk(text: str) -> None:
            await _fire(self._bus, HookEvent.THINK, text=text, run_id=run.run_id)
            token_events.append(ThinkTokenEvent(token=text, run_id=run.run_id))

        # The time budget is a hard deadline on the call, not an afterthought.
        llm_timeout = max(0.1, self._budget.max_seconds - run.elapsed_seconds())

        llm_config = self._llm_config
        if llm_config is not None and self._cache_boundary is not None:
            llm_config = dataclasses.replace(
                llm_config, cache_boundary_index=self._cache_boundary()
            )

        coro = self._llm.complete(
            messages,
            system=system,
            tools=tools or None,
            config=llm_config,
            on_chunk=_on_chunk,
        )
        try:
            response = await asyncio.wait_for(coro, timeout=llm_timeout)
        except TimeoutError as exc:
            raise BudgetExceeded(
                f"LLM call timed out after {llm_timeout:.1f}s.",
                run_id=run.run_id,
                axis="seconds",
                value=run.elapsed_seconds(),
                limit=self._budget.max_seconds,
            ) from exc
        return response, token_events

    async def _account(self, run: AgentRun, response: LLMResponse) -> None:
        run.metrics.turn_count += 1
        if not getattr(response, "from_cache", False):
            run.add_tokens(
                response,
                cost_usd=self._llm_config.cost_for_response(response)
                if self._llm_config is not None
                else 0.0,
            )

        if response.reasoning_content:
            await _fire(
                self._bus, HookEvent.THINK, text=response.reasoning_content, run_id=run.run_id
            )

        await _fire(
            self._bus,
            HookEvent.TOKEN_UPDATE,
            turn=run.turn_count,
            input_tokens=response.input_tokens,
            output_tokens=response.output_tokens,
            total_tokens=response.total_tokens,
            cache_read_tokens=response.cache_read_tokens,
            cache_write_tokens=response.cache_write_tokens,
            cache_hit_ratio=response.cache_read_tokens / max(1, response.input_tokens),
            cost_usd=run.cost_usd,
            budget_usd=self._budget.max_cost_usd,
            max_turns=self._budget.max_turns,
            elapsed_s=run.elapsed_seconds(),
            max_seconds=self._budget.max_seconds,
            model=response.model,
            run_id=run.run_id,
        )

        if response.content or response.tool_calls:
            msg: Message = {"role": "assistant", "content": response.content}
            if response.tool_calls:
                msg["tool_calls"] = [
                    {
                        "id": tc.call_id,
                        "type": "function",
                        "function": {
                            "name": tc.name,
                            "arguments": json.dumps(tc.params),
                        },
                    }
                    for tc in response.tool_calls
                ]
            run.messages.append(msg)

    def _end_turn(self, run: AgentRun, response: LLMResponse) -> bool:
        """True when the model stopped without asking for tools — run is done."""
        if response.stop_reason != StopReason.END_TURN and response.tool_calls:
            return False

        run.state = RunState.COMPLETED
        run.final_output = response.content
        if not run.final_output:
            for msg in reversed(run.messages):
                if msg.get("role") == "assistant" and msg.get("content"):
                    content = msg["content"]
                    run.final_output = content if isinstance(content, str) else str(content)
                    break
        logger.info(
            "Step[%s] completed in %d turns (%.2fs, $%.4f)",
            run.run_id,
            run.turn_count,
            run.elapsed_seconds(),
            run.cost_usd,
        )
        return True


async def _fire(bus: HookRegistry | None, event: HookEvent, **payload: Any) -> None:
    if bus is not None:
        await bus.fire(event, **payload)
