"""ReactEngine — the Turn loop, as the execution of a ReActBody.

This is the old REACTIVE loop's heartbeat, lifted out of being *an
executor* and folded into being *a body*: drive one run
think→decide→execute, Turn after Turn, until a terminal flag lands on the
run (COMPLETED / SUSPENDED / pending_handoff). Everything *around* the
loop — where the run came from, run scoping, settling, terminal stream
events — moved up to the :class:`~prodagent.plan.scheduler.Scheduler`;
an engine never emits a run-terminal event, it just stops.

Resume is exact: a run suspended awaiting approval replays the parked
call — the very call shown to the human — instead of asking the model
again.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from prodagent.base.config import LoopConfig
from prodagent.base.event_log import Event, RunEventType
from prodagent.kernel.budget import SAFETY_NET_BUDGET, check_spawn_budget
from prodagent.kernel.progress import ProgressMonitor
from prodagent.kernel.turn import Turn
from prodagent.kernel.types import (
    AgentEvent,
    MessageList,
    RunState,
    ToolOutcome,
    ToolResult,
)

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator

    from prodagent.base.time_recorder import RecordingTimePort
    from prodagent.cognition.context.manager import ContextManager
    from prodagent.cognition.context.spill import ToolResultSpillStore
    from prodagent.kernel.budget import BudgetLedger, HardBudget
    from prodagent.kernel.bus import HookRegistry
    from prodagent.kernel.state import AgentRun
    from prodagent.llm import LLMClient
    from prodagent.ports import CheckpointStore, EventLog
    from prodagent.tooling.dispatcher import ToolDispatcher

logger = logging.getLogger(__name__)

__all__ = ["ReactEngine"]

_TERMINAL_CURSOR = "terminal"
"""The marker-stream tail: run-terminal markers chain on this cursor."""


class ReactEngine:
    """Turn-after-Turn execution of one run — what a ReActBody runs."""

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
        self._clock: RecordingTimePort | None = None
        self._last_answer: str = ""
        cfg = loop_config or LoopConfig()
        self.progress = ProgressMonitor(
            stall_threshold=cfg.stall_threshold,
            repeat_threshold=cfg.repeat_threshold,
            window_size=cfg.fingerprint_window,
        )
        """The dead-loop guard — the factory attaches this to the dispatcher's
        batch config for REACTIVE runs (Turns fingerprint through it)."""
        self._turn = self._build_turn(llm, dispatcher)

    def _build_turn(self, llm: LLMClient, dispatcher: ToolDispatcher) -> Turn:
        cm = self._context_manager

        async def _assemble(run: AgentRun) -> tuple[str, MessageList]:
            assert cm is not None and self._dispatcher is not None
            return await cm.prepare(
                run, hooks=self._hooks, invoked_skills=self._dispatcher.invoked_skills()
            )

        return Turn(
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

    def _check_budget(self, run: AgentRun) -> None:
        check_spawn_budget(run, self._budget, self._budget_ledger)

    def bind_clock(self, clock: RecordingTimePort | None) -> None:
        """The Scheduler owns the run's frozen-clock recorder; the engine
        flushes it ahead of each marker so replay order holds."""
        self._clock = clock

    async def drive(
        self, run: AgentRun, *, goal: str | None = None, settle_run: bool = True
    ) -> AsyncGenerator[AgentEvent, None]:
        """The heartbeat: parked-call replay (if resuming a suspension),
        then Turn after Turn until a terminal flag lands on the run — or,
        in goal scope, until the model finishes the goal.

        ``settle_run`` is the whole-run vs one-node distinction: reactive
        runs complete the RUN on the model's final answer; a goal node
        mid-graph finishes the NODE and leaves the run to the waves. The
        Turn reports finishing; this loop decides what it settles."""
        if goal and (not run.messages or run.messages[-1].get("content") != goal):
            # Idempotent seeding: a re-run node (resume, retry) must not
            # stack duplicate goal messages on the shared transcript.
            from prodagent.kernel.types import Message

            run.messages.append(Message(role="user", content=goal))
        park = run.clear_approval_park()
        if park is not None:
            # Resuming a SUSPENDED run: retry the exact call awaiting approval
            # instead of asking the LLM again.
            self._dispatcher.set_pending_approval_id(park.request_id)
            async for batch_evt in self._dispatcher.run_batch(run, [park.call]):
                yield batch_evt

            await self._record_turn(run)

            self._check_budget(run)

            if run.state is RunState.SUSPENDED:
                return

        while True:
            async for event in self._turn.run(
                run, system=self._system, tools=self._tools_schema or None
            ):
                yield event

            await self._record_turn(run)

            if run.pending_handoff is not None:
                return
            if run.state is RunState.SUSPENDED:
                return
            if self._turn.finished:
                self._last_answer = self._turn.answer
                if settle_run:
                    run.complete(self._turn.answer, backfill=True)
                    logger.info(
                        "Turn[%s] completed in %d turns (%.2fs, $%.4f)",
                        run.run_id,
                        run.turn_count,
                        run.elapsed_seconds(),
                        run.cost_usd,
                    )
                return
            if run.state is RunState.COMPLETED:
                return

    def outcome_of(self, run: AgentRun, *, goal_scope: bool = False) -> ToolResult:
        """The run's terminal flag, as a node outcome — how a finished ReActBody
        reports into the node lifecycle (success / suspended / handoff)."""
        if run.pending_handoff is not None:
            h = run.pending_handoff
            return ToolResult.for_handoff(peer=h.peer_name, task=h.task, tool="react")
        if run.state is RunState.SUSPENDED:
            return ToolResult.suspended(
                reason="awaiting approval",
                tool="react",
                approval_request_id=run.pending_approval_id or "",
            )
        value = self._last_answer if goal_scope else (run.final_output or "")
        return ToolResult(ToolOutcome.OK, value=value, tool="react")

    async def _record_turn(self, run: AgentRun) -> None:
        """Per-turn event + checkpoint — a no-op unless both stores are
        configured, so an unconfigured (or checkpoint-only) run behaves
        exactly as before: no per-turn writes."""
        if self._event_log is None or self._checkpoint_store is None:
            return
        if self._clock is not None:
            await self._clock.flush(self._event_log)
        seq = await self._event_log.append(
            Event.make(RunEventType.TURN_COMPLETED, stream_id=run.run_id, version=0),
            expected_seq=run.cursor(_TERMINAL_CURSOR, 0),
        )
        run.set_cursor(_TERMINAL_CURSOR, seq)
        from prodagent.kernel.bus import save_and_fire_checkpoint

        await save_and_fire_checkpoint(self._checkpoint_store, run, self._hooks)

    async def record_terminal(self, run: AgentRun, event_type: RunEventType) -> None:
        """Terminal marker, mirrored by the Scheduler before its final
        checkpoint — appended on the same chain the turn markers use."""
        if self._event_log is None:
            return
        if self._clock is not None:
            await self._clock.flush(self._event_log)
        seq = await self._event_log.append(
            Event.make(event_type, stream_id=run.run_id, version=0),
            expected_seq=run.cursor(_TERMINAL_CURSOR, 0),
        )
        run.set_cursor(_TERMINAL_CURSOR, seq)
