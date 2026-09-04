"""AgentLoop — an agent's inner loop, and Round, its atom.

A Round is the atom of agency: one model call plus at most one tool
batch. AgentLoop is the policy for iterating Rounds — when to stop, what
to resume, how to settle: drive one run think→decide→execute, Round
after Round, until a terminal flag lands on the run (COMPLETED /
SUSPENDED), a handoff tool transfers control, or a goal finishes.
Everything *around* the loop — where the run came from, run scoping,
settling, terminal stream events — lives in the kernel's Scheduler; a
loop never emits a run-terminal event, it just stops.

The kernel sees none of this machinery: the loop body lives in the recipes
layer (``runtime/recipes/loop_body``) and drives whatever implements the
``LoopDriver`` port it declares — this class is that implementation. The
implementation is composition: llm, dispatcher, optional context manager,
stores and hooks arrive from the caller.

Resume is exact: a run suspended awaiting approval replays the parked
call — the very call shown to the human — instead of asking the model
again.
"""

from __future__ import annotations

import asyncio
import dataclasses
import json
import logging
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

from prodagent.base.config import LoopConfig
from prodagent.base.errors import BudgetExceeded
from prodagent.base.event_log import Event, RunEventType
from prodagent.base.time_recorder import RecordingTimePort
from prodagent.kernel.budget import SAFETY_NET_BUDGET, check_spawn_budget
from prodagent.kernel.bus import HookEvent, HookRegistry
from prodagent.kernel.progress import ProgressMonitor
from prodagent.kernel.types import (
    AgentEvent,
    LLMResponse,
    Message,
    MessageList,
    RunState,
    StopReason,
    ThinkTokenEvent,
    ToolOutcome,
    ToolResult,
    ToolResultEvent,
)

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator, AsyncIterator, Awaitable, Callable

    from prodagent.cognition.context.manager import ContextManager
    from prodagent.cognition.context.spill import ToolResultSpillStore
    from prodagent.kernel.budget import BudgetLedger, HardBudget
    from prodagent.kernel.run import Run
    from prodagent.kernel.scheduler import Scheduler
    from prodagent.kernel.types import ToolCall
    from prodagent.ports import CheckpointStore, EventLog
    from prodagent.ports.llm import LLMClient, LLMConfig
    from prodagent.tooling.dispatcher import ToolDispatcher

logger = logging.getLogger(__name__)

__all__ = ["AgentLoop", "Round", "ContextAssembler", "ToolRunner", "ProgressGuard"]


# ════════════ the atom ════════════


@runtime_checkable
class ContextAssembler(Protocol):
    """Prepares what the model sees this round: ``(system, messages)``."""

    def __call__(self, run: Run) -> Awaitable[tuple[str, MessageList]]: ...


@runtime_checkable
class ToolRunner(Protocol):
    """Executes a batch of tool calls against a run, yielding events."""

    def run_batch(self, run: Run, calls: list[ToolCall]) -> AsyncIterator[AgentEvent]: ...


@runtime_checkable
class ProgressGuard(Protocol):
    """Dead-loop detection over the run's fingerprint window."""

    def check(self, run: Run) -> None: ...


class Round:
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
        budget_check: Callable[[Run], None] | None = None,
        llm_config: LLMConfig | None = None,
        cache_boundary: Callable[[], int | None] | None = None,
        phase: str = "loop",
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
        """The model's final content of the finished round (backfill-free:
        the loop decides whether to backfill)."""

    def _check_budget(self, run: Run) -> None:
        if self._budget_check is not None:
            self._budget_check(run)

    async def run(
        self,
        run: Run,
        *,
        system: str,
        tools: list[dict[str, Any]] | None,
    ) -> AsyncIterator[AgentEvent]:
        """One round of the atom: think (assemble → call → account), then —
        only if the model asked for tools — act, with budget checked on both
        sides of the batch (the model call itself may have burned the cap)."""
        self.finished = False
        self.answer = ""
        response, token_events = await self._think(run, system=system, tools=tools)
        for evt in token_events:
            yield evt
        if self._end_round(run, response):
            return
        self._check_budget(run)
        async for event in self._runner.run_batch(run, response.tool_calls):
            yield event
        self._check_budget(run)

    async def _think(
        self,
        run: Run,
        *,
        system: str,
        tools: list[dict[str, Any]] | None,
    ) -> tuple[LLMResponse, list[ThinkTokenEvent]]:
        system, messages = await self._prepare(run, system=system)
        response, token_events = await self._call_llm(run, system, messages, tools)
        await self._account(run, response)
        return response, token_events

    async def _prepare(self, run: Run, *, system: str) -> tuple[str, MessageList]:
        self._check_budget(run)
        if self._guard is not None:
            self._guard.check(run)

        await _fire(
            self._bus,
            HookEvent.ROUND_START,
            round=run.turn_count + 1,
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
        run: Run,
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

    async def _account(self, run: Run, response: LLMResponse) -> None:
        run.metrics.turn_count += 1
        # A cached response is a replay — its tokens were already accounted on
        # first execution — but the round still counts: the turns axis must
        # see a run that spins on cache hits, not one that looks free.
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
            round=run.turn_count,
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
            # Skip fully-empty assistant rounds — some providers emit them on
            # tool-only responses, and an empty one pollutes the transcript.
            msg: Message = {"role": "assistant", "content": response.content}
            if response.thinking_blocks:
                # Raw blocks ride on the message so a tool-use continuation can
                # re-send them (Anthropic rejects the round without them).
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

    def _end_round(self, run: Run, response: LLMResponse) -> bool:
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


# ════════════ the loop ════════════


class AgentLoop:
    """Round-after-Round execution of one run — what a recipes LoopBody runs."""

    def __init__(
        self,
        llm: LLMClient,
        dispatcher: ToolDispatcher,
        *,
        system_prompt: str = "",
        tools_schema: list[dict[str, Any]] | None = None,
        budget: HardBudget | None = None,
        checkpoint_store: CheckpointStore | None = None,
        event_log: EventLog | None = None,
        context_manager: ContextManager | None = None,
        hooks: HookRegistry | None = None,
        loop_config: LoopConfig | None = None,
        spill_store: ToolResultSpillStore | None = None,
        budget_ledger: BudgetLedger | None = None,
    ) -> None:
        self._system = system_prompt
        self._tools_schema = tools_schema or []
        self._budget = budget or SAFETY_NET_BUDGET
        self._checkpoint_store = checkpoint_store
        self._event_log = event_log
        self._context_manager = context_manager
        self._hooks = hooks
        self._dispatcher = dispatcher
        self._budget_ledger = budget_ledger
        self._last_answer: str = ""
        self._handoff: ToolResult | None = None
        """A handoff tool result seen this drive — the loop's stop flag and
        what ``outcome_of`` folds into the node outcome (nothing parks on
        the run; control transfer is a command the scheduler applies)."""
        cfg = loop_config or LoopConfig()
        self.progress = ProgressMonitor(
            stall_threshold=cfg.stall_threshold,
            repeat_threshold=cfg.repeat_threshold,
            window_size=cfg.fingerprint_window,
        )
        """The dead-loop guard — the factory attaches this to the dispatcher's
        batch config (Rounds fingerprint through it)."""
        self._round = self._build_round(llm, dispatcher)

    def _build_round(self, llm: LLMClient, dispatcher: ToolDispatcher) -> Round:
        cm = self._context_manager

        async def _assemble(run: Run) -> tuple[str, MessageList]:
            assert cm is not None and self._dispatcher is not None
            return await cm.prepare(
                run, hooks=self._hooks, invoked_skills=self._dispatcher.invoked_skills()
            )

        return Round(
            llm,
            dispatcher,
            budget=self._budget,
            guard=self.progress,
            bus=self._hooks,
            assembler=_assemble if cm is not None else None,
            budget_check=self._check_budget,
            llm_config=getattr(llm, "default_config", None),
            cache_boundary=(lambda: cm.cache_boundary_index) if cm is not None else None,
        )

    def _check_budget(self, run: Run) -> None:
        check_spawn_budget(run, self._budget, self._budget_ledger)

    async def drive(
        self, run: Run, *, goal: str | None = None, settle_run: bool = True
    ) -> AsyncGenerator[AgentEvent, None]:
        """The heartbeat: parked-call replay (if resuming a suspension),
        then Round after Round until a terminal flag lands on the run — or,
        in goal scope, until the model finishes the goal.

        ``settle_run`` is the whole-run vs one-node distinction: a bare
        agent run completes the RUN on the model's final answer; an
        autonomous node mid-graph finishes the NODE and leaves the run to
        the waves. The Round reports finishing; this loop decides what it
        settles."""
        if goal and (not run.messages or run.messages[-1].get("content") != goal):
            # Idempotent seeding: a re-run node (resume, retry) must not
            # stack duplicate goal messages on the shared transcript.
            from prodagent.kernel.types import Message

            run.messages.append(Message(role="user", content=goal))
        iv = run.take_interrupt()
        if iv is not None:
            # Resuming a SUSPENDED run: retry the exact call awaiting approval
            # instead of asking the LLM again.
            staged = iv.staged_call()
            self._dispatcher.set_pending_approval_id(iv.request_id or None)
            if staged is not None:
                async for batch_evt in self._dispatcher.run_batch(run, [staged]):
                    yield batch_evt

            await self._record_round(run)

            self._check_budget(run)

            if run.state is RunState.SUSPENDED:
                return

        self._handoff = None
        while True:
            async for event in self._round.run(
                run, system=self._system, tools=self._tools_schema or None
            ):
                if (
                    isinstance(event, ToolResultEvent)
                    and isinstance(result := event.result, ToolResult)
                    and result.outcome is ToolOutcome.HANDOFF
                ):
                    # Control leaves this agent: stop driving — the node's
                    # outcome folds this into a Handoff command for the
                    # scheduler to apply.
                    self._handoff = result
                yield event

            await self._record_round(run)

            if self._handoff is not None:
                return
            if run.state is RunState.SUSPENDED:
                return
            if self._round.finished:
                self._last_answer = self._round.answer
                if settle_run:
                    run.complete(self._round.answer, backfill=True)
                    logger.info(
                        "Loop[%s] completed in %d turns (%.2fs, $%.4f)",
                        run.run_id,
                        run.turn_count,
                        run.elapsed_seconds(),
                        run.cost_usd,
                    )
                return
            if run.state is RunState.COMPLETED:
                return

    def outcome_of(self, run: Run, *, goal_scope: bool = False) -> ToolResult:
        """The run's terminal flag, as a node outcome — how a finished
        LoopBody reports into the node lifecycle (success / suspended /
        handoff)."""
        if self._handoff is not None:
            h = self._handoff.handoff or {}
            return ToolResult.for_handoff(
                peer=str(h.get("peer", "")), task=str(h.get("task", "")), tool="loop"
            )
        if run.state is RunState.SUSPENDED:
            return ToolResult.suspended(
                reason="awaiting approval",
                tool="loop",
                approval_request_id=run.pending_approval_id or "",
            )
        value = self._last_answer if goal_scope else (run.final_output or "")
        return ToolResult(ToolOutcome.OK, value=value, tool="loop")

    @staticmethod
    def _active_clock() -> RecordingTimePort | None:
        """The run's frozen-clock recorder, when one is active.

        The Scheduler owns the recorder and scopes it over the stream (a
        ``value_override`` contextvar); the loop reads it here so its
        markers flush recorded time facts ahead of themselves — replay
        order holds without anyone handing us a clock."""
        from prodagent.base.determinism import current_time

        port = current_time()
        return port if isinstance(port, RecordingTimePort) else None

    async def _record_round(self, run: Run) -> None:
        """Per-round event + checkpoint — a no-op unless both stores are
        configured, so an unconfigured (or checkpoint-only) run behaves
        exactly as before: no per-round writes."""
        if self._event_log is None or self._checkpoint_store is None:
            return
        clock = self._active_clock()
        if clock is not None:
            await clock.flush(self._event_log)
        seq = await self._event_log.append(
            Event.make(RunEventType.ROUND_COMPLETED, stream_id=run.run_id, version=0),
            expected_seq=run.marker_tail(),
        )
        # The marker stream is shared with the plan executor's events (a
        # graph's work node rounds interleave with node markers), so the
        # tail advances both boxes — the next plan event expects what this
        # append left, and vice versa.
        run.advance_marker_tail(seq)
        from prodagent.kernel.bus import save_and_fire_checkpoint

        await save_and_fire_checkpoint(self._checkpoint_store, run, self._hooks)

    async def record_terminal(self, run: Run, event_type: RunEventType) -> None:
        """Terminal marker, mirrored by the Scheduler before its final
        checkpoint — appended on the same chain the round markers use."""
        if self._event_log is None:
            return
        clock = self._active_clock()
        if clock is not None:
            await clock.flush(self._event_log)
        seq = await self._event_log.append(
            Event.make(event_type, stream_id=run.run_id, version=0),
            expected_seq=run.marker_tail(),
        )
        run.advance_marker_tail(seq)


async def _fire(bus: HookRegistry | None, event: HookEvent, **payload: Any) -> None:
    if bus is not None:
        await bus.fire(event, **payload)


def agent_scheduler(
    llm: LLMClient,
    dispatcher: ToolDispatcher,
    *,
    system_prompt: str = "",
    tools_schema: list[dict[str, Any]] | None = None,
    budget: HardBudget | None = None,
    checkpoint_store: CheckpointStore | None = None,
    event_log: EventLog | None = None,
    context_manager: ContextManager | None = None,
    hooks: HookRegistry | None = None,
    loop_config: LoopConfig | None = None,
    spill_store: ToolResultSpillStore | None = None,
    initial_messages: MessageList | None = None,
    budget_ledger: BudgetLedger | None = None,
) -> Scheduler:
    """A standalone agent run, assembled: the loop engine plus the Scheduler,
    the agent itself as a one-node graph. The composition root for tests and
    the replay deck; the factory builds the same shape per hop."""
    from prodagent.base.config import ContextConfig
    from prodagent.kernel.scheduler import Scheduler
    from prodagent.runtime.recipes.loop_body import LOOP_DRIVER_KEY, LoopBody

    engine = AgentLoop(
        llm,
        dispatcher,
        system_prompt=system_prompt,
        tools_schema=tools_schema,
        budget=budget,
        checkpoint_store=checkpoint_store,
        event_log=event_log,
        context_manager=context_manager,
        hooks=hooks,
        loop_config=loop_config,
        spill_store=spill_store,
        budget_ledger=budget_ledger,
    )
    resolved_spill = spill_store or (context_manager.spill_store if context_manager else None)
    dispatcher.configure_batch(
        loop_config=loop_config,
        context_config=(
            context_manager.config
            if context_manager is not None
            else (ContextConfig() if resolved_spill is not None else None)
        ),
        spill_store=resolved_spill,
        progress_monitor=engine.progress,
    )
    return Scheduler(
        system=system_prompt,
        initial_messages=initial_messages,
        hooks=hooks,
        budget=budget,
        event_log=event_log,
        checkpoint_store=checkpoint_store,
        budget_ledger=budget_ledger,
        dispatcher=dispatcher,
        wiring={LOOP_DRIVER_KEY: engine},
        terminal_marker=engine.record_terminal,
        initial_body=LoopBody(),
    )
