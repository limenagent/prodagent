"""Plan-then-execute mode for agent phases."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from prodagent.base.errors import LLMError
from prodagent.hooks import fire as _fire
from prodagent.kernel.budget import check_spawn_budget
from prodagent.kernel.bus import HookEvent
from prodagent.kernel.types import (
    MessageList,
    RunState,
    StepCompletedEvent,
    StepFailedEvent,
    StepStartedEvent,
    StepStatus,
)
from prodagent.plan.bootstrap import PlanBootstrap
from prodagent.plan.event_log import PlanEventLog
from prodagent.plan.finalize import finalize_run, terminal_event
from prodagent.plan.planner import Planner
from prodagent.plan.step_runner import (
    StepFailed,
    StepHandoff,
    StepOutcome,
    StepRunner,
    StepSuccess,
    StepSuspended,
    ToolExecutor,
    commit_transcript,
)

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator, Iterator

    from prodagent.kernel.budget import BudgetLedger, HardBudget
    from prodagent.kernel.bus import HookRegistry
    from prodagent.kernel.state import AgentRun
    from prodagent.kernel.types import AgentEvent
    from prodagent.llm import LLMClient
    from prodagent.plan.dag import Plan, PlanStep
    from prodagent.ports import CheckpointStore, EventLog
    from prodagent.tooling.dispatcher import ToolDispatcher

logger = logging.getLogger(__name__)

_MAX_ITERS_PER_STEP = 3
_MAX_ITERS_SLOP = 5

__all__ = ["PlanExecutor"]


@dataclass(slots=True)
class _BatchResult:
    successes: list[StepSuccess] = field(default_factory=list)
    failures: list[StepFailed] = field(default_factory=list)
    suspended: StepSuspended | None = None
    handoff: StepHandoff | None = None


class PlanExecutor:
    """The ``ExecutionMode.PLAN_FIRST`` leaf executor: commit to a plan, then run it.

    Builds a DAG of steps upfront (or accepts one via ``initial_plan``), executes
    steps respecting their dependencies, and replans (up to ``max_replans`` times)
    when a step fails. Contrast with
    :class:`~prodagent.kernel.loop.ReactiveLoop`, the other leaf executor,
    which has no upfront plan and decides one action at a time.
    """

    def __init__(
        self,
        llm: LLMClient,
        tool_executor: ToolExecutor,
        *,
        system: str = "",
        messages: MessageList | None = None,
        hooks: HookRegistry | None = None,
        agent_name: str = "",
        max_replans: int = 2,
        tool_schemas: list[dict[str, Any]] | None = None,
        event_log: EventLog | None = None,
        checkpoint_store: CheckpointStore | None = None,
        framework_config: Any = None,
        budget: HardBudget | None = None,
        initial_plan: Plan | None = None,
        budget_ledger: BudgetLedger | None = None,
        dispatcher: ToolDispatcher | None = None,
    ) -> None:
        self._llm = llm
        self._system = system
        self._messages = list(messages) if messages else []
        self._hooks = hooks
        self._agent_name = agent_name
        self._tool_schemas = tool_schemas or []
        self._budget = budget
        self._budget_ledger = budget_ledger
        self._max_replans = max_replans
        if event_log is None:
            from prodagent.backends.factory import in_memory_event_log

            event_log = in_memory_event_log()
        if checkpoint_store is None:
            from prodagent.backends.factory import in_memory_checkpoint_store

            checkpoint_store = in_memory_checkpoint_store()
        self._log = PlanEventLog(
            event_log=event_log,
            checkpoint_store=checkpoint_store,
            hooks=hooks,
        )
        self._planner = Planner(
            llm=llm,
            config=None,
            tool_schemas=self._tool_schemas,
            hooks=hooks,
            framework_config=framework_config,
        )
        self._step_runner = StepRunner(
            tool_executor,
            self._log,
            hooks=hooks,
            agent_name=agent_name,
            dispatcher=dispatcher,
        )
        self._dispatcher = dispatcher
        self._bootstrap = PlanBootstrap(
            self._log,
            self._planner,
            system=system,
            messages=self._messages,
            hooks=hooks,
            agent_name=agent_name,
            initial_plan=initial_plan,
            dispatcher=dispatcher,
            check_budget=self._check_budget,
        )
        self._replan_count = 0

    def _check_budget(self, run: AgentRun) -> None:
        check_spawn_budget(run, self._budget, self._budget_ledger)

    async def stream(
        self,
        task: str,
        *,
        run_id: str | None = None,
        parent_run_id: str | None = None,
    ) -> AsyncGenerator[AgentEvent, None]:
        run, plan = await self._bootstrap.prepare(task, run_id, parent_run_id=parent_run_id)
        if plan is not None:
            plan = await self._bootstrap.gate(plan, run)
        if plan is not None:
            async for event in self._execute_plan_events(plan, run):
                yield event
        finalize_run(run, plan)
        yield terminal_event(run)

    async def _execute_plan_events(
        self,
        plan: Plan,
        run: AgentRun,
    ) -> AsyncGenerator[AgentEvent, None]:
        # No while-True over an LLM-authored graph: step count × replans
        # bounds the loop, with a little slop for replan churn.
        max_iterations = len(plan.steps) * _MAX_ITERS_PER_STEP + _MAX_ITERS_SLOP

        for _ in range(max_iterations):
            if plan.is_complete():
                return
            self._check_budget(run)
            ready = plan.get_parallel_ready()
            if not ready:
                return

            for s in ready:
                yield StepStartedEvent(step_id=s.step_id, action=s.action, run_id=run.run_id)

            outcomes = await self._dispatch_batch(ready, plan, run)
            batch = self._classify_outcomes(outcomes)

            for event in self._emit_step_events(batch, run.run_id):
                yield event

            if batch.handoff is not None:
                return
            if batch.suspended is not None:
                return

            self._check_budget(run)

            if batch.failures and await self._handle_failures(batch.failures, plan, run):
                return

    async def _dispatch_batch(
        self,
        ready: list[PlanStep],
        plan: Plan,
        run: AgentRun,
    ) -> list[StepOutcome]:
        """Dispatch ready steps with the same ordering discipline as the
        REACTIVE batch: readonly steps run concurrently (bounded), write
        steps run one at a time — two HIGH side-effect tools must never
        race just because the DAG unblocked them together. A suspension or
        handoff stops the batch: the run is already waiting on a human or a
        peer, firing more side effects would be wrong. Failures don't stop
        it — matching REACTIVE, where a failed write lets its siblings run."""
        by_step: dict[str, StepOutcome] = {}

        readonly = [s for s in ready if self._step_is_readonly(s)]
        writes = [s for s in ready if s.step_id not in {r.step_id for r in readonly}]

        if readonly:
            raw = await asyncio.gather(
                *[self._step_runner.run_one(s, plan, run) for s in readonly],
                return_exceptions=True,
            )
            for step, r in zip(readonly, raw, strict=True):
                if isinstance(r, asyncio.CancelledError):
                    raise r
                if isinstance(r, BaseException):
                    by_step[step.step_id] = StepFailed(step=step, error=r)
                else:
                    by_step[step.step_id] = r

        for step in writes:
            if run.state is not RunState.RUNNING or run.pending_handoff is not None:
                break
            try:
                by_step[step.step_id] = await self._step_runner.run_one(step, plan, run)
            except asyncio.CancelledError:
                raise
            except BaseException as exc:  # noqa: BLE001 — a step failure is data, not a crash
                by_step[step.step_id] = StepFailed(step=step, error=exc)

        # Steps never launched (batch stopped by suspend/handoff) are omitted —
        # they stay PENDING in the plan and re-run after resume.
        outcomes = [by_step[s.step_id] for s in ready if s.step_id in by_step]
        for oc in outcomes:
            if isinstance(oc, StepSuccess):
                commit_transcript(oc.step, oc, run)
        return outcomes

    def _step_is_readonly(self, step: PlanStep) -> bool:
        if self._dispatcher is None:
            return False  # no metadata → treat everything as a write (serial, safe)
        meta = self._dispatcher.meta_for(step.action)
        return meta.is_readonly if meta is not None else False

    @staticmethod
    def _classify_outcomes(outcomes: list[StepOutcome]) -> _BatchResult:
        batch = _BatchResult()
        for oc in outcomes:
            match oc:
                case StepSuccess():
                    batch.successes.append(oc)
                case StepFailed():
                    batch.failures.append(oc)
                case StepSuspended() if batch.suspended is None:
                    batch.suspended = oc
                case StepHandoff() if batch.handoff is None:
                    batch.handoff = oc
        return batch

    @staticmethod
    def _emit_step_events(
        batch: _BatchResult,
        run_id: str,
    ) -> Iterator[AgentEvent]:
        if batch.handoff is not None or batch.suspended is not None:
            for succ in batch.successes:
                yield StepCompletedEvent(
                    step_id=succ.step.step_id,
                    action=succ.step.action,
                    result=succ.step.output_ref,
                    run_id=run_id,
                )
            return
        for succ in batch.successes:
            yield StepCompletedEvent(
                step_id=succ.step.step_id,
                action=succ.step.action,
                result=succ.step.output_ref,
                run_id=run_id,
            )
        for fail in batch.failures:
            yield StepFailedEvent(
                step_id=fail.step.step_id,
                action=fail.step.action,
                error=str(fail.error),
                run_id=run_id,
            )

    async def _handle_failures(
        self,
        failures: list[StepFailed],
        plan: Plan,
        run: AgentRun,
    ) -> bool:
        primary, *secondary = failures
        for fail in secondary:
            await self._record_failure(fail, plan, run, replan=False)
        return await self._record_failure(primary, plan, run)

    async def _record_failure(
        self,
        failure: StepFailed,
        plan: Plan,
        run: AgentRun,
        *,
        replan: bool = True,
    ) -> bool:
        step = failure.step
        exc = failure.error
        step.status = StepStatus.FAILED
        step.error = str(exc)
        run.tool_failures += 1
        obsoleted = plan.mark_downstream_obsolete(step.step_id)
        await self._log.record_step_failed(plan, run, step.step_id, str(exc))
        await _fire(
            self._hooks,
            HookEvent.STEP_FAILED,
            plan_id=run.run_id,
            step_id=step.step_id,
            action=step.action,
            error=str(exc),
            run_id=run.run_id,
        )
        logger.error("[Plan] step=%s FAILED: %s  obsoleted=%s", step.step_id, exc, obsoleted)

        if not replan:
            return False
        if self._replan_count >= self._max_replans:
            logger.error("[Plan] max_replans=%d reached — stopping", self._max_replans)
            return True

        try:
            new_steps = await self._planner.replan(
                plan, step, str(exc), self._system, run.messages, run
            )
        except LLMError as replan_exc:
            logger.error("[Plan] replan LLM call failed: %s", replan_exc)
            run.state = RunState.FAILED
            run.last_error = str(replan_exc)
            return True
        if not new_steps:
            logger.warning("[Plan] replan #%d produced no steps — stopping", self._replan_count + 1)
            return True

        plan.merge(new_steps)
        self._replan_count += 1
        await self._log.record_replanned(plan, run, new_steps)
        await _fire(
            self._hooks,
            HookEvent.PLAN_REPLANNED,
            plan_id=plan.plan_id,
            version=plan.version,
            failed_step=step.step_id,
            new_steps=[
                {
                    "id": s.step_id,
                    "action": s.action,
                    "params": s.params,
                    "depends_on": s.depends_on,
                }
                for s in new_steps
            ],
            replan_count=self._replan_count,
            run_id=run.run_id,
        )
        logger.info(
            "[Plan] replan #%d → V%d  new_steps=%s",
            self._replan_count,
            plan.version,
            [s.step_id for s in new_steps],
        )
        return False
