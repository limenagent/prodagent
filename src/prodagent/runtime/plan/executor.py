"""Plan-then-execute mode for agent phases."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from prodagent.core.events import (
    StepCompletedEvent,
    StepFailedEvent,
    StepStartedEvent,
)
from prodagent.core.exceptions import LLMError
from prodagent.core.types import MessageList, RunState, StepStatus
from prodagent.hooks import fire as _fire
from prodagent.hooks.events import HookEvent
from prodagent.runtime.coordination.accounting import check_spawn_budget
from prodagent.runtime.plan.bootstrap import PlanBootstrap
from prodagent.runtime.plan.event_log import PlanEventLog
from prodagent.runtime.plan.finalize import finalize_run, terminal_event
from prodagent.runtime.plan.planner import Planner
from prodagent.runtime.plan.step_runner import (
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

    from prodagent.core.budget import HardBudget
    from prodagent.core.events import AgentEvent
    from prodagent.core.state.run import AgentRun
    from prodagent.hooks.registry import HookRegistry
    from prodagent.llm.base import LLMClient
    from prodagent.ports import CheckpointStore, EventLog
    from prodagent.runtime.coordination.accounting import SpawnAccumulator
    from prodagent.runtime.plan.dag import Plan, PlanStep
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
    """Execute a phase in plan-then-execute mode."""

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
        spawn_accumulators: list[SpawnAccumulator] | None = None,
        initial_plan: Plan | None = None,
        dispatcher: ToolDispatcher | None = None,
    ) -> None:
        self._llm = llm
        self._system = system
        self._messages = list(messages) if messages else []
        self._hooks = hooks
        self._agent_name = agent_name
        self._tool_schemas = tool_schemas or []
        self._budget = budget
        self._spawn_accumulators = spawn_accumulators or []
        self._max_replans = max_replans
        self._log = PlanEventLog(
            event_log=event_log,
            checkpoint_store=checkpoint_store,
            framework_config=framework_config,
            hooks=hooks,
        )
        self._planner = Planner(
            llm=llm,
            config=None,
            tool_schemas=self._tool_schemas,
            hooks=hooks,
            framework_config=framework_config,
        )
        self._step_runner = StepRunner(tool_executor, self._log, hooks=hooks, agent_name=agent_name)
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
        self._dispatcher = dispatcher
        self._replan_count = 0

    def _check_budget(self, run: AgentRun) -> None:
        check_spawn_budget(run, self._budget, self._spawn_accumulators)

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
        raw = await asyncio.gather(
            *[self._step_runner.run_one(s, plan, run) for s in ready],
            return_exceptions=True,
        )
        outcomes: list[StepOutcome] = []
        for step, r in zip(ready, raw, strict=True):
            if isinstance(r, asyncio.CancelledError):
                raise r
            if isinstance(r, BaseException):
                outcomes.append(StepFailed(step=step, error=r))
            else:
                outcomes.append(r)

        for step, oc in zip(ready, outcomes, strict=True):
            if isinstance(oc, StepSuccess):
                commit_transcript(step, oc, run)
        return outcomes

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
