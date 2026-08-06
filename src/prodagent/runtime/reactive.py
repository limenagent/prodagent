"""ReactiveLoop — the REACTIVE leaf executor."""

from __future__ import annotations

import asyncio
import json
import logging
import uuid
from typing import TYPE_CHECKING, Any

from prodagent.core.budget import HardBudget
from prodagent.core.config import LoopConfig
from prodagent.core.events import (
    AgentEvent,
    RunCompletedEvent,
    RunFailedEvent,
    RunSuspendedEvent,
    ThinkTokenEvent,
)
from prodagent.core.exceptions import BudgetExceeded, InfiniteLoopDetected
from prodagent.core.progress import ProgressMonitor
from prodagent.core.state.run import AgentRun
from prodagent.core.types import (
    LLMResponse,
    Message,
    MessageList,
    RunPhase,
    RunState,
    StopReason,
)
from prodagent.hooks import fire as _fire
from prodagent.hooks import save_and_fire_checkpoint
from prodagent.hooks.events import HookEvent
from prodagent.llm.base import LLMConfig
from prodagent.runtime.coordination.accounting import check_spawn_budget
from prodagent.tooling.runner import ToolRunner

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator

    from prodagent.cognition.context.manager import ContextManager
    from prodagent.cognition.context.spill import ToolResultSpillStore
    from prodagent.hooks.registry import HookRegistry
    from prodagent.llm.base import LLMClient
    from prodagent.ports import CheckpointStore
    from prodagent.runtime.coordination.accounting import SpawnAccumulator
    from prodagent.tooling.dispatcher import ToolDispatcher

logger = logging.getLogger(__name__)


class ReactiveLoop:
    """Greedy think→decide→execute loop: the ``ExecutionMode.REACTIVE`` leaf executor."""

    def __init__(
        self,
        llm: LLMClient,
        dispatcher: ToolDispatcher,
        *,
        system_prompt: str = "",
        tools_schema: list[dict[str, Any]] | None = None,
        budget: HardBudget | None = None,
        checkpoint_store: CheckpointStore | None = None,
        context_manager: ContextManager | None = None,
        hooks: HookRegistry | None = None,
        loop_config: LoopConfig | None = None,
        spill_store: ToolResultSpillStore | None = None,
        spawn_accumulators: list[SpawnAccumulator] | None = None,
        initial_messages: MessageList | None = None,
    ) -> None:
        self._llm = llm
        self._system = system_prompt
        self._tools_schema = tools_schema or []
        self._budget = budget or HardBudget()
        self._llm_config = LLMConfig()
        self._checkpoint_store = checkpoint_store
        self._context_manager = context_manager
        self._initial_messages = list(initial_messages) if initial_messages else None
        self._hooks = hooks
        self._loop_config = loop_config
        self._dispatcher = dispatcher
        self._spawn_accumulators = spawn_accumulators or []
        resolved_spill_store = spill_store or (
            context_manager.spill_store if context_manager else None
        )
        cfg = loop_config or LoopConfig()
        self._progress = ProgressMonitor(
            stall_threshold=cfg.stall_threshold,
            repeat_threshold=cfg.repeat_threshold,
            window_size=cfg.fingerprint_window,
        )
        self._runner = ToolRunner(
            dispatcher,
            loop_config=loop_config,
            context_config=context_manager.config if context_manager else None,
            spill_store=resolved_spill_store,
            progress_monitor=self._progress,
        )

    async def stream(
        self,
        task: str,
        *,
        run_id: str | None = None,
        parent_run_id: str | None = None,
    ) -> AsyncGenerator[AgentEvent, None]:
        run = await self._resolve_run(task, run_id=run_id, parent_run_id=parent_run_id)
        logger.info("ReactiveLoop[%s] stream started: %r", run.run_id, task[:80])
        await self._begin_run_span(run, task)

        try:
            async for event in self._loop_events(run):
                yield event
        except BudgetExceeded as exc:
            yield await self._settle_terminated(run, exc)
        except InfiniteLoopDetected as exc:
            yield await self._settle_terminated(run, exc)
        except Exception as exc:
            await self._settle_unexpected(run, exc)
            raise
        else:
            await self._end_run_span(run)
        finally:
            if self._checkpoint_store is not None:
                await save_and_fire_checkpoint(self._checkpoint_store, run, self._hooks)

    async def _settle_terminated(
        self, run: AgentRun, exc: BudgetExceeded | InfiniteLoopDetected
    ) -> AgentEvent:
        run.state = RunState.FAILED
        self._record_fault(run, exc)
        await self._end_run_span(run, error=str(exc))
        logger.warning("ReactiveLoop[%s] terminated: %s", run.run_id, exc)
        return RunFailedEvent(run=run, error=str(exc))

    async def _settle_unexpected(self, run: AgentRun, exc: BaseException) -> None:
        run.state = RunState.FAILED
        self._record_fault(run, exc)
        await self._end_run_span(run, error=str(exc))
        logger.exception("ReactiveLoop[%s] unexpected error", run.run_id)

    @staticmethod
    def _record_fault(run: AgentRun, exc: BaseException) -> None:
        run.last_error = str(exc)

    @staticmethod
    def _prune_unresolved_tool_uses(run: AgentRun) -> None:
        msgs = run.messages
        if not msgs:
            return
        last = msgs[-1]
        if last.get("role") == "assistant" and run.pending_tool_call is not None:
            msgs.pop()
        run.pending_tool_call = None

    async def _begin_run_span(self, run: AgentRun, task: str) -> None:
        await _fire(
            self._hooks,
            HookEvent.LOOP_START,
            run_id=run.run_id,
            task=task[:200],
        )

    async def _end_run_span(self, run: AgentRun, *, error: str | None = None) -> None:
        await _fire(self._hooks, HookEvent.LOOP_END, run_id=run.run_id, error=error)

    def _check_budget(self, run: AgentRun) -> None:
        check_spawn_budget(run, self._budget, self._spawn_accumulators)

    async def _loop_events(
        self,
        run: AgentRun,
    ) -> AsyncGenerator[AgentEvent, None]:
        # Resuming a SUSPENDED run: retry the exact call awaiting approval instead of asking the LLM again.
        if run.pending_tool_call is not None:
            resumed_call = run.pending_tool_call
            run.pending_tool_call = None
            self._dispatcher.set_pending_approval_id(run.pending_approval_id)
            run.pending_approval_id = None
            run.phase = RunPhase.EXECUTE
            async for batch_evt in self._runner.run_batch(run, [resumed_call]):
                yield batch_evt

            self._check_budget(run)

            if run.state is RunState.SUSPENDED:
                yield RunSuspendedEvent(run=run)
                return

        while True:
            run.phase = RunPhase.THINK
            response, token_events = await self._think(run)
            for evt in token_events:
                yield evt

            run.phase = RunPhase.DECIDE
            done = await self._decide(run, response)
            if done:
                run.phase = RunPhase.DONE
                yield RunCompletedEvent(run=run)
                return

            self._check_budget(run)

            run.phase = RunPhase.EXECUTE
            async for batch_evt in self._runner.run_batch(run, response.tool_calls):
                yield batch_evt

            self._check_budget(run)

            if run.pending_handoff is not None:
                run.phase = RunPhase.DONE
                yield RunCompletedEvent(run=run)
                return

            if run.state is RunState.SUSPENDED:
                yield RunSuspendedEvent(run=run)
                return

    async def _think(
        self,
        run: AgentRun,
    ) -> tuple[LLMResponse, list[ThinkTokenEvent]]:
        system, messages_to_send = await self._pre_llm_checks(run)
        response, token_events = await self._call_llm(run, system, messages_to_send)
        await self._post_llm_accounting(run, response)
        return response, token_events

    async def _pre_llm_checks(self, run: AgentRun) -> tuple[str, list[Message]]:
        run.phase = RunPhase.PREPARE
        self._check_budget(run)
        self._progress.check(run)

        await _fire(
            self._hooks,
            HookEvent.TURN_START,
            turn=run.turn_count + 1,
            max_turns=self._budget.max_turns,
            run_id=run.run_id,
        )

        if self._context_manager:
            system, messages_to_send = await self._context_manager.prepare(
                run, hooks=self._hooks, invoked_skills=self._dispatcher.invoked_skills()
            )
        else:
            system = self._system
            messages_to_send = run.messages

        await _fire(
            self._hooks,
            HookEvent.LLM_REQUEST,
            system=system[:200],
            system_len=len(system),
            messages=messages_to_send,
            msg_count=len(messages_to_send),
            phase="reactive",
            run_id=run.run_id,
        )
        return system, messages_to_send

    async def _call_llm(
        self,
        run: AgentRun,
        system: str,
        messages_to_send: list[Message],
    ) -> tuple[LLMResponse, list[ThinkTokenEvent]]:
        token_events: list[ThinkTokenEvent] = []
        hooks = self._hooks

        async def _on_chunk(text: str) -> None:
            if hooks is not None:
                await _fire(hooks, HookEvent.THINK, text=text, run_id=run.run_id)
            token_events.append(ThinkTokenEvent(token=text, run_id=run.run_id))

        llm_timeout: float | None = None
        if self._budget is not None:
            elapsed = run.elapsed_seconds()
            remaining = self._budget.max_seconds - elapsed
            llm_timeout = max(0.1, remaining)

        coro = self._llm.complete(
            messages_to_send,
            system=system,
            tools=self._tools_schema or None,
            config=self._llm_config,
            on_chunk=_on_chunk,
        )
        try:
            response = (
                await asyncio.wait_for(coro, timeout=llm_timeout)
                if llm_timeout is not None
                else await coro
            )
        except TimeoutError as exc:
            raise BudgetExceeded(
                f"LLM call timed out after {llm_timeout:.1f}s.",
                run_id=run.run_id,
                axis="seconds",
                value=run.elapsed_seconds(),
                limit=self._budget.max_seconds if self._budget else 0.0,
            ) from exc
        return response, token_events

    async def _post_llm_accounting(
        self,
        run: AgentRun,
        response: LLMResponse,
    ) -> None:
        run.metrics.turn_count += 1
        if not getattr(response, "from_cache", False):
            run.add_tokens(response, cost_usd=self._llm_config.cost_for_response(response))

        if response.reasoning_content and self._hooks is not None:
            await _fire(
                self._hooks,
                HookEvent.THINK,
                text=response.reasoning_content,
                run_id=run.run_id,
            )

        await _fire(
            self._hooks,
            HookEvent.TOKEN_UPDATE,
            turn=run.turn_count,
            input_tokens=response.input_tokens,
            output_tokens=response.output_tokens,
            total_tokens=response.total_tokens,
            cost_usd=run.cost_usd,
            budget_usd=self._budget.max_cost_usd if self._budget else 0,
            max_turns=self._budget.max_turns if self._budget else 0,
            elapsed_s=run.elapsed_seconds(),
            max_seconds=self._budget.max_seconds if self._budget else 0,
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

    async def _decide(self, run: AgentRun, response: LLMResponse) -> bool:
        if response.stop_reason != StopReason.END_TURN and response.tool_calls:
            return False

        run.state = RunState.COMPLETED
        if response.content:
            run.final_output = response.content
        elif run.messages:
            for msg in reversed(run.messages):
                if msg.get("role") == "assistant" and msg.get("content"):
                    content = msg["content"]
                    run.final_output = content if isinstance(content, str) else str(content)
                    break
        logger.info(
            "ReactiveLoop[%s] completed in %d turns (%.2fs, $%.4f)",
            run.run_id,
            run.turn_count,
            run.elapsed_seconds(),
            run.cost_usd,
        )
        return True

    def _init_run(
        self,
        task: str,
        *,
        run_id: str | None,
        parent_run_id: str | None = None,
    ) -> AgentRun:
        resolved_run_id = run_id or str(uuid.uuid4())
        run = AgentRun(run_id=resolved_run_id, task=task, parent_run_id=parent_run_id)
        run.messages.append(Message(role="user", content=task))
        return run

    async def _resolve_run(
        self,
        task: str,
        *,
        run_id: str | None,
        parent_run_id: str | None = None,
    ) -> AgentRun:
        if self._initial_messages is not None:
            resolved_run_id = run_id or str(uuid.uuid4())
            run = AgentRun(run_id=resolved_run_id, task=task, parent_run_id=parent_run_id)
            run.messages = list(self._initial_messages)
            logger.info(
                "ReactiveLoop[%s] chat turn: %d seeded messages",
                resolved_run_id,
                len(run.messages),
            )
            return run

        if self._checkpoint_store is not None and run_id:
            existing = await self._checkpoint_store.load(run_id)
            if existing is not None:
                if existing.state is not RunState.SUSPENDED:
                    self._prune_unresolved_tool_uses(existing)
                existing.state = RunState.RUNNING
                existing.last_error = None
                logger.info(
                    "ReactiveLoop[%s] resuming from checkpoint: %d messages, turn=%d",
                    run_id,
                    len(existing.messages),
                    existing.turn_count,
                )
                return existing
        return self._init_run(task, run_id=run_id, parent_run_id=parent_run_id)
