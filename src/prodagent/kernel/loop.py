"""The REACTIVE loop — a policy for iterating Steps.

The turn atom (assemble → call model → account → act) lives in
:class:`prodagent.kernel.step.Step`; this class owns everything *around* the
atom — run resolution and resume, loop spans, termination, settling, and
checkpointing. Termination flags land on the run (``COMPLETED`` /
``SUSPENDED`` / ``pending_handoff``); the loop only translates them into
terminal events.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from prodagent.base.config import ContextConfig, LoopConfig
from prodagent.base.determinism import new_uuid4
from prodagent.base.errors import BudgetExceeded, InfiniteLoopDetected
from prodagent.base.event_log import Event, RunEventType
from prodagent.base.run_context import run_scope
from prodagent.kernel.budget import SAFETY_NET_BUDGET, check_spawn_budget
from prodagent.kernel.bus import HookEvent, save_and_fire_checkpoint
from prodagent.kernel.bus import fire as _fire
from prodagent.kernel.progress import ProgressMonitor
from prodagent.kernel.state import AgentRun
from prodagent.kernel.step import Step
from prodagent.kernel.types import (
    AgentEvent,
    Message,
    MessageList,
    RunCompletedEvent,
    RunFailedEvent,
    RunState,
    RunSuspendedEvent,
)

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator

    from prodagent.cognition.context.manager import ContextManager
    from prodagent.cognition.context.spill import ToolResultSpillStore
    from prodagent.kernel.budget import BudgetLedger, HardBudget
    from prodagent.kernel.bus import HookRegistry
    from prodagent.llm import LLMClient
    from prodagent.ports import CheckpointStore, EventLog
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
        event_log: EventLog | None = None,
        context_manager: ContextManager | None = None,
        hooks: HookRegistry | None = None,
        loop_config: LoopConfig | None = None,
        spill_store: ToolResultSpillStore | None = None,
        initial_messages: MessageList | None = None,
        budget_ledger: BudgetLedger | None = None,
    ) -> None:
        self._system = system_prompt
        self._tools_schema = tools_schema or []
        self._budget = budget or SAFETY_NET_BUDGET
        self._checkpoint_store = checkpoint_store
        self._event_log = event_log
        self._context_manager = context_manager
        self._initial_messages = list(initial_messages) if initial_messages else None
        self._hooks = hooks
        self._dispatcher = dispatcher
        self._budget_ledger = budget_ledger
        resolved_spill_store = spill_store or (
            context_manager.spill_store if context_manager else None
        )
        # The dispatcher needs the ContextConfig for spill truncation even
        # when the ContextManager itself is off (spill without compression).
        if context_manager is not None:
            tool_context_config: ContextConfig | None = context_manager.config
        else:
            tool_context_config = ContextConfig() if resolved_spill_store else None
        cfg = loop_config or LoopConfig()
        self._progress = ProgressMonitor(
            stall_threshold=cfg.stall_threshold,
            repeat_threshold=cfg.repeat_threshold,
            window_size=cfg.fingerprint_window,
        )
        dispatcher.configure_batch(
            loop_config=loop_config,
            context_config=tool_context_config,
            spill_store=resolved_spill_store,
            progress_monitor=self._progress,
        )
        self._step = self._build_step(llm, dispatcher)

    def _build_step(self, llm: LLMClient, dispatcher: ToolDispatcher) -> Step:
        cm = self._context_manager

        async def _assemble(run: AgentRun) -> tuple[str, MessageList]:
            assert cm is not None and self._dispatcher is not None
            return await cm.prepare(
                run, hooks=self._hooks, invoked_skills=self._dispatcher.invoked_skills()
            )

        return Step(
            llm,
            dispatcher,
            budget=self._budget,
            guard=self._progress,
            bus=self._hooks,
            assembler=_assemble if cm is not None else None,
            budget_check=self._check_budget,
            llm_config=getattr(llm, "default_config", None),
            cache_boundary=(lambda: cm.cache_boundary_index) if cm is not None else None,
        )

    async def stream(
        self,
        task: str,
        *,
        run_id: str | None = None,
        parent_run_id: str | None = None,
    ) -> AsyncGenerator[AgentEvent, None]:
        """Drive the loop to a terminal state and always emit exactly one
        terminal event. Budget/loop deaths settle into ``RunFailedEvent``;
        unexpected exceptions settle the run *and* re-raise (the caller
        learns what broke, the checkpoint records that it broke); the
        ``finally`` checkpoint fires on every path."""
        run = await self._resolve_run(task, run_id=run_id, parent_run_id=parent_run_id)
        logger.info("ReactiveLoop[%s] stream started: %r", run.run_id, task[:80])
        await self._begin_run_span(run, task)

        # Boundary facts attribute to this run: the recorder wrapping the LLM
        # client reads the identity from here (base.run_context).
        with run_scope(run.run_id):
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
                await self._record_terminal(
                    run,
                    RunEventType.RUN_SUSPENDED
                    if run.state is RunState.SUSPENDED
                    else RunEventType.RUN_COMPLETED,
                )
            finally:
                if self._checkpoint_store is not None:
                    await save_and_fire_checkpoint(self._checkpoint_store, run, self._hooks)

    async def _settle_terminated(
        self, run: AgentRun, exc: BudgetExceeded | InfiniteLoopDetected
    ) -> AgentEvent:
        run.fail(exc)
        await self._end_run_span(run, error=str(exc))
        await self._record_terminal(run, RunEventType.RUN_FAILED)
        logger.warning("ReactiveLoop[%s] terminated: %s", run.run_id, exc)
        return RunFailedEvent(run=run, error=str(exc))

    async def _settle_unexpected(self, run: AgentRun, exc: BaseException) -> None:
        run.fail(exc)
        await self._end_run_span(run, error=str(exc))
        await self._record_terminal(run, RunEventType.RUN_FAILED)
        logger.exception("ReactiveLoop[%s] unexpected error", run.run_id)

    @staticmethod
    def _prune_unresolved_tool_uses(run: AgentRun) -> None:
        # A crash mid-batch leaves an assistant turn whose tool call never got
        # a result; providers reject a replayed tool_use without its
        # tool_result, so the dangling turn is dropped and re-thought.
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
        check_spawn_budget(run, self._budget, self._budget_ledger)

    async def _record_turn(self, run: AgentRun) -> None:
        """Per-turn event + checkpoint — REACTIVE's counterpart to PLAN_FIRST's
        per-step ``_record``. A no-op unless both stores are configured, so an
        unconfigured (or checkpoint-only) loop behaves exactly as it does
        today: no per-turn writes, just the single checkpoint in ``stream()``'s
        ``finally``."""
        if self._event_log is None or self._checkpoint_store is None:
            return
        seq = await self._event_log.append(
            Event.make(RunEventType.TURN_COMPLETED, stream_id=run.run_id, version=0),
            expected_seq=run.cursor("reactive", 0),
        )
        run.set_cursor("reactive", seq)
        await save_and_fire_checkpoint(self._checkpoint_store, run, self._hooks)

    async def _record_terminal(self, run: AgentRun, event_type: RunEventType) -> None:
        """Terminal marker mirroring PLAN_FIRST's STEP_COMPLETED/FAILED/SUSPENDED —
        appended before ``stream()``'s ``finally`` writes the last checkpoint."""
        if self._event_log is None:
            return
        seq = await self._event_log.append(
            Event.make(event_type, stream_id=run.run_id, version=0),
            expected_seq=run.cursor("reactive", 0),
        )
        run.set_cursor("reactive", seq)

    async def _loop_events(
        self,
        run: AgentRun,
    ) -> AsyncGenerator[AgentEvent, None]:
        """The heartbeat: parked-call replay (if resuming a suspension),
        then Step after Step until a terminal flag lands on the run."""
        # Resuming a SUSPENDED run: retry the exact call awaiting approval instead of asking the LLM again.
        park = run.clear_approval_park()
        if park is not None:
            self._dispatcher.set_pending_approval_id(park.request_id)
            async for batch_evt in self._dispatcher.run_batch(run, [park.call]):
                yield batch_evt

            await self._record_turn(run)

            self._check_budget(run)

            if run.state is RunState.SUSPENDED:
                yield RunSuspendedEvent(run=run)
                return

        while True:
            async for event in self._step.run(
                run, system=self._system, tools=self._tools_schema or None
            ):
                yield event

            await self._record_turn(run)

            if run.pending_handoff is not None:
                yield RunCompletedEvent(run=run)
                return
            if run.state is RunState.COMPLETED:
                yield RunCompletedEvent(run=run)
                return
            if run.state is RunState.SUSPENDED:
                yield RunSuspendedEvent(run=run)
                return

    def _init_run(
        self,
        task: str,
        *,
        run_id: str | None,
        parent_run_id: str | None = None,
    ) -> AgentRun:
        resolved_run_id = run_id or new_uuid4()
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
        """Where a run comes from: seeded messages (chat turn) → checkpoint
        resume (dangling tool turns pruned, crash scene cleared) → fresh.
        Order matters — a chat turn never accidentally resumes a checkpoint."""
        if self._initial_messages is not None:
            resolved_run_id = run_id or new_uuid4()
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
                existing.revive()
                logger.info(
                    "ReactiveLoop[%s] resuming from checkpoint: %d messages, turn=%d",
                    run_id,
                    len(existing.messages),
                    existing.turn_count,
                )
                return existing
        return self._init_run(task, run_id=run_id, parent_run_id=parent_run_id)
