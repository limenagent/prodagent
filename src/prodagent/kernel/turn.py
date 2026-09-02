"""Turn — the atom of agency: one model call plus at most one tool round.

A REACTIVE loop is nothing but a policy for iterating Turns (when to stop,
what to resume, how to settle). Everything the Turn needs from the world
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

from prodagent.base.errors import BudgetExceeded
from prodagent.kernel.bus import HookEvent, HookRegistry
from prodagent.kernel.types import (
    LLMResponse,
    Message,
    MessageList,
    StopReason,
    ThinkTokenEvent,
)

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Awaitable, Callable

    from prodagent.kernel.budget import HardBudget
    from prodagent.kernel.state import AgentRun
    from prodagent.kernel.types import AgentEvent, ToolCall
    from prodagent.ports.llm import LLMClient, LLMConfig

logger = logging.getLogger(__name__)

__all__ = ["Turn", "ContextAssembler", "ToolRunner", "ProgressGuard"]


@runtime_checkable
class ContextAssembler(Protocol):
    """Prepares what the model sees this turn: ``(system, messages)``."""

    def __call__(self, run: AgentRun) -> Awaitable[tuple[str, MessageList]]: ...


@runtime_checkable
class ToolRunner(Protocol):
    """Executes a batch of tool calls against a run, yielding events."""

    def run_batch(self, run: AgentRun, calls: list[ToolCall]) -> AsyncIterator[AgentEvent]: ...


@runtime_checkable
class ProgressGuard(Protocol):
    """Dead-loop detection over the run's fingerprint window."""

    def check(self, run: AgentRun) -> None: ...


class Turn:
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
        self.finished = False
        """Set when the model's last answer asked for no tools — read by the
        driving loop, which owns what "finished" settles."""
        self.answer = ""
        """The model's final content of the finished turn (backfill-free:
        the loop decides whether to backfill)."""

    def _check_budget(self, run: AgentRun) -> None:
        if self._budget_check is not None:
            self._budget_check(run)

    async def run(
        self,
        run: AgentRun,
        *,
        system: str,
        tools: list[dict[str, Any]] | None,
    ) -> AsyncIterator[AgentEvent]:
        """One turn of the atom: think (assemble → call → account), then —
        only if the model asked for tools — act, with budget checked on both
        sides of the batch (the model call itself may have burned the cap)."""
        self.finished = False
        self.answer = ""
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
        # A cached response is a replay — its tokens were already accounted on
        # first execution — but the turn still counts: the turns axis must see
        # a run that spins on cache hits, not one that looks free.
        if not getattr(response, "from_cache", False):  # getattr: non-caching clients lack the flag
            run.add_tokens(
                response,
                cost_usd=self._llm_config.cost_for_response(response)
                if self._llm_config is not None
                else 0.0,
            )

        if response.reasoning_content:
            # Non-streaming path still surfaces the reasoning as one THINK
            # event, so observers see it exactly once either way.
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
            # Skip fully-empty assistant turns — some providers emit them on
            # tool-only responses, and an empty turn pollutes the transcript.
            msg: Message = {"role": "assistant", "content": response.content}
            if response.thinking_blocks:
                # Raw blocks ride on the message so a tool-use continuation can
                # re-send them (Anthropic rejects the turn without them).
                msg["thinking"] = [dict(b) for b in response.thinking_blocks]
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
        """True when the model stopped without asking for tools. The atom
        only REPORTS the finish (``finished`` / ``answer``); who the finish
        settles — the whole run, or just this node — is the driving loop's
        call. The tool_calls guard matters: some providers report
        END_TURN-ish stops *with* pending calls, and dropping those would
        strand a tool_use with no result."""
        if response.stop_reason != StopReason.END_TURN and response.tool_calls:
            return False

        self.finished = True
        self.answer = response.content or ""
        return True


async def _fire(bus: HookRegistry | None, event: HookEvent, **payload: Any) -> None:
    if bus is not None:
        await bus.fire(event, **payload)
