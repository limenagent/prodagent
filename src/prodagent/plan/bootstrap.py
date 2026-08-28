"""PlanBootstrap — plan preparation, resumption, and the HITL approval gate."""

from __future__ import annotations

import copy
import logging
import uuid
from typing import TYPE_CHECKING, Any

from prodagent.base.errors import LLMError, SuspendPendingApproval
from prodagent.hooks import fire as _fire
from prodagent.kernel.bus import Gate, HookEvent
from prodagent.kernel.state import AgentRun
from prodagent.kernel.types import RunState
from prodagent.plan.dag import Plan

if TYPE_CHECKING:
    from collections.abc import Callable

    from prodagent.kernel.bus import HookRegistry
    from prodagent.kernel.types import MessageList
    from prodagent.plan.event_log import PlanEventLog
    from prodagent.plan.planner import Planner
    from prodagent.tooling.dispatcher import ToolDispatcher

logger = logging.getLogger(__name__)


def _steps_to_hook_dict(
    steps: list[Any], *, include_terminal: bool = False
) -> list[dict[str, Any]]:
    return [s.to_hook_dict(include_terminal=include_terminal) for s in steps]


class PlanBootstrap:
    """Resolves the initial (run, plan) pair and gates it through HITL approval."""

    def __init__(
        self,
        log: PlanEventLog,
        planner: Planner,
        *,
        system: str = "",
        messages: MessageList | None = None,
        hooks: HookRegistry | None = None,
        agent_name: str = "",
        initial_plan: Plan | None = None,
        dispatcher: ToolDispatcher | None = None,
        check_budget: Callable[[AgentRun], None],
    ) -> None:
        self._log = log
        self._planner = planner
        self._system = system
        self._messages = list(messages) if messages else []
        self._hooks = hooks
        self._agent_name = agent_name
        self._initial_plan = initial_plan
        self._dispatcher = dispatcher
        self._check_budget = check_budget

    async def prepare(
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
            # A preset workflow plan is a reusable template — deep-copy so one
            # run's statuses/replans never bleed into the next turn's plan.
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

    async def gate(self, plan: Plan, run: AgentRun) -> Plan | None:
        if self._hooks is None:
            return plan
        try:
            veto = await self._hooks.check_blocking(
                Gate.PLAN_APPROVAL,
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
