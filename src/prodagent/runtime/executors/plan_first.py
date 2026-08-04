"""Plan-then-execute mode for agent phases."""

from __future__ import annotations

import asyncio
import copy
import logging
import uuid
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from prodagent.core.events import (
    AgentEvent,
    RunCompletedEvent,
    RunFailedEvent,
    RunSuspendedEvent,
    StepCompletedEvent,
    StepFailedEvent,
    StepStartedEvent,
)
from prodagent.core.exceptions import LLMError, SuspendPendingApproval
from prodagent.core.state.run import AgentRun
from prodagent.core.types import MessageList, RunState, StepStatus
from prodagent.hooks import fire as _fire
from prodagent.hooks.checkpoint import CheckPoint
from prodagent.hooks.events import HookEvent
from prodagent.runtime.coordination.comm import check_spawn_budget
from prodagent.runtime.plan.dag import Plan, PlanStep
from prodagent.runtime.plan.event_log import PlanEventLog
from prodagent.runtime.plan.planner import Planner
from prodagent.runtime.plan.step_runner import (
    StepFailed,
    StepHandoff,
    StepOutcome,
    StepRunner,
    StepSuccess,
    StepSuspended,
    ToolExecutor,
    _format_step_output,
    commit_transcript,
)

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator, Iterator

    from prodagent.core.budget import HardBudget
    from prodagent.hooks.registry import HookRegistry
    from prodagent.llm.base import LLMClient
    from prodagent.ports import CheckpointStore, EventLog
    from prodagent.runtime.coordination.comm import SpawnAccumulator
    from prodagent.tooling.dispatcher import ToolDispatcher

logger = logging.getLogger(__name__)

_MAX_ITERS_PER_STEP = 3
_MAX_ITERS_SLOP = 5

__all__ = ["PlanExecutor"]


def _steps_to_hook_dict(
    steps: list[PlanStep], *, include_terminal: bool = False
) -> list[dict[str, Any]]:
    """One shared shape for step payloads in plan gates / PLAN_READY events."""
    return [s.to_hook_dict(include_terminal=include_terminal) for s in steps]


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
        self._initial_plan = initial_plan
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
        run, plan = await self._prepare_run(task, run_id, parent_run_id=parent_run_id)
        if plan is not None:
            plan = await self._gate_plan(plan, run)
        if plan is not None:
            async for event in self._execute_plan_events(plan, run):
                yield event
        self._finalize_run(run, plan)
        yield self._terminal_event(run)

    async def _gate_plan(self, plan: Plan, run: AgentRun) -> Plan | None:
        if self._hooks is None:
            return plan
        try:
            veto = await self._hooks.check_blocking(
                CheckPoint.PLAN_APPROVAL,
                plan_id=plan.plan_id,
                version=plan.version,
                agent=self._agent_name,
                steps=_steps_to_hook_dict(plan.steps, include_terminal=True),
                run_id=run.run_id,
                pending_approval_id=run.pending_approval_id,
            )
        except SuspendPendingApproval as exc:
            run.state = RunState.SUSPENDED
            run.pending_approval_id = exc.request_id
            run.last_error = f"plan suspended pending approval: {exc}"
            await self._log.save_snapshot(run, plan=plan)
            logger.info(
                "[Plan] plan=%s SUSPENDED for HITL review (request_id=%s)",
                plan.plan_id,
                exc.request_id,
            )
            return None
        if veto.blocked:
            run.state = RunState.FAILED
            run.last_error = veto.reason or "plan rejected by HITL reviewer"
            run.pending_approval_id = None
            logger.info("[Plan] plan=%s REJECTED by HITL — run fails", plan.plan_id)
            return None
        run.pending_approval_id = None
        return plan

    async def _prepare_run(
        self, task: str, run_id: str | None, *, parent_run_id: str | None = None
    ) -> tuple[AgentRun, Plan | None]:
        rid = run_id or str(uuid.uuid4())
        run = AgentRun(run_id=rid, task=task, parent_run_id=parent_run_id)
        run.messages = list(self._messages)

        if await self._log.has_resumable_state(rid):
            state = await self._log.restore_plan(run)
            plan_a: Plan = Plan.from_state(state, plan_id=rid)
            plan_a.task_input = task
            if run.pending_approval_id is not None:
                if self._dispatcher is not None:
                    self._dispatcher.set_pending_approval_id(run.pending_approval_id)
                plan_a.requeue_suspended()
            logger.info(
                "[Plan] resuming run=%s — %d step(s), v%d",
                rid,
                len(plan_a.steps),
                plan_a.version,
            )
            return run, plan_a

        await self._log.rebaseline_checkpoint(run)

        plan: Plan | None = None
        if self._initial_plan is not None:
            plan = copy.deepcopy(self._initial_plan)
            plan.plan_id = rid
            plan.task_input = task
            await self._log.record_plan_created(plan, run)
            await _fire(
                self._hooks,
                HookEvent.PLAN_READY,
                plan_id=plan.plan_id,
                version=plan.version,
                agent=self._agent_name,
                steps=_steps_to_hook_dict(plan.steps),
                run_id=run.run_id,
            )
            logger.info(
                "[Plan] using hand-written workflow plan=%s — %d step(s)", rid, len(plan.steps)
            )
            self._check_budget(run)
            return run, plan

        plan = await self._generate_plan(task, rid, run)
        if plan is None:
            if not run.last_error:
                logger.warning("[PlanExecutor] Failed to parse plan JSON — no steps to execute")
                run.state = RunState.FAILED
                run.last_error = "Failed to parse plan JSON — no steps to execute"
        else:
            self._check_budget(run)
        return run, plan

    async def _generate_plan(self, task: str, rid: str, run: AgentRun) -> Plan | None:
        try:
            draft = await self._planner.generate(task, self._system, self._messages, run)
        except LLMError as exc:
            logger.error("[PlanExecutor] planning LLM call failed: %s", exc)
            run.state = RunState.FAILED
            run.last_error = str(exc)
            return None
        if draft.plan is None:
            return None
        plan = draft.plan
        plan.plan_id = rid
        plan.task_input = task
        run.messages.append({"role": "assistant", "content": draft.raw_text})
        await self._log.record_plan_created(plan, run)
        await _fire(
            self._hooks,
            HookEvent.PLAN_READY,
            plan_id=plan.plan_id,
            version=plan.version,
            agent=self._agent_name,
            steps=_steps_to_hook_dict(plan.steps),
            run_id=run.run_id,
        )
        return plan

    @staticmethod
    def _terminal_event(run: AgentRun) -> AgentEvent:
        if run.state is RunState.SUSPENDED:
            return RunSuspendedEvent(run=run)
        if run.state is RunState.FAILED:
            return RunFailedEvent(run=run, error=run.last_error or "")
        return RunCompletedEvent(run=run)

    @staticmethod
    def _finalize_run(run: AgentRun, plan: Plan | None) -> None:
        if run.state is RunState.RUNNING:
            run.state = RunState.COMPLETED
        if run.pending_handoff is not None:
            return
        if plan is None:
            return

        terminal = next(
            (
                s.output_ref
                for s in plan.steps
                if s.is_terminal and s.status is StepStatus.COMPLETED
            ),
            None,
        )
        if terminal is not None:
            run.final_output = _format_step_output(terminal)
            return

        sink = PlanExecutor._select_terminal_step(plan)
        if sink is not None:
            run.final_output = _format_step_output(sink.output_ref)

    @staticmethod
    def _select_terminal_step(plan: Plan) -> PlanStep | None:
        completed = [s for s in plan.steps if s.status is StepStatus.COMPLETED]
        if not completed:
            return None
        timed = [s for s in completed if s.completed_at > 0]
        if timed:
            return max(timed, key=lambda s: s.completed_at)
        return completed[-1]

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
