"""PlanBootstrap — run/plan preparation, resumption, and the HITL approval gate.

Where a run comes from, in source order: a resumable event-log state (crash
recovery), a preset Workflow plan, the LLM planner — or, for REACTIVE, the
degenerate single-node plan plus checkpoint-based resume. Keeping the choice
here means the scheduler starts every run the same way: with a (run, plan)
pair in hand.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from prodagent.base.determinism import new_uuid4
from prodagent.base.errors import LLMError, SuspendPendingApproval
from prodagent.base.types import ExecutionMode
from prodagent.hooks import fire as _fire
from prodagent.kernel.bus import Gate, HookEvent
from prodagent.kernel.state import AgentRun
from prodagent.plan.dag import Plan, react_plan
from prodagent.plan.ir.compiler import compile_planned
from prodagent.plan.ir.validator import PlanValidationError

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable

    from prodagent.kernel.bus import HookRegistry
    from prodagent.kernel.types import MessageList
    from prodagent.plan.dag import Node
    from prodagent.plan.event_log import PlanEventLog
    from prodagent.plan.planner import PlanDraft, Planner
    from prodagent.ports import CheckpointStore
    from prodagent.tooling.dispatcher import ToolDispatcher

logger = logging.getLogger(__name__)


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


def _nodes_to_hook_dict(
    nodes: Iterable[Node], *, include_terminal: bool = False
) -> list[dict[str, Any]]:
    return [n.to_hook_dict(include_terminal=include_terminal) for n in nodes]


class PlanBootstrap:
    """Resolves the initial (run, plan) pair and gates it through HITL approval."""

    def __init__(
        self,
        log: PlanEventLog | None,
        planner: Planner | None,
        *,
        mode: ExecutionMode = ExecutionMode.PLAN_FIRST,
        system: str = "",
        initial_messages: MessageList | None = None,
        hooks: HookRegistry | None = None,
        agent_name: str = "",
        initial_plan: Plan | None = None,
        dispatcher: ToolDispatcher | None = None,
        check_budget: Callable[[AgentRun], None],
        checkpoint_store: CheckpointStore | None = None,
        depth: int = 0,
    ) -> None:
        self._log = log
        self._planner = planner
        self._mode = mode
        self._system = system
        self._initial_messages = list(initial_messages) if initial_messages else None
        self._hooks = hooks
        self._agent_name = agent_name
        self._initial_plan = initial_plan
        self._dispatcher = dispatcher
        self._check_budget = check_budget
        self._checkpoint_store = checkpoint_store
        self._depth = depth

    async def prepare(
        self, task: str, run_id: str | None, *, parent_run_id: str | None = None
    ) -> tuple[AgentRun, Plan | None]:
        """Resolve the initial (run, plan) pair — new or resumed, caller never
        cares which.

        Three sources in priority order: a resumable event-log state (crash
        recovery), a preset Workflow plan (hand-written, model never plans),
        and the LLM planner (the model drafts the DAG). Keeping the choice
        here means the executor starts every run the same way: with a (run,
        plan) pair in hand."""
        rid = run_id or new_uuid4()

        if self._mode is ExecutionMode.REACTIVE:
            run = await self._resolve_reactive_run(task, rid, parent_run_id=parent_run_id)
            return run, react_plan(plan_id=rid)

        run = AgentRun(run_id=rid, task=task, parent_run_id=parent_run_id, depth=self._depth)
        assert self._log is not None  # plan mode always wires a PlanEventLog
        run.messages = list(self._initial_messages or [{"role": "user", "content": ""}])

        if await self._log.has_resumable_state(rid):
            state = await self._log.restore_plan(run)
            plan_a, node_states = Plan.from_state(state, plan_id=rid)
            plan_a.task_input = task
            run.node_states = node_states
            if run.pending_approval_id is not None:
                if self._dispatcher is not None:
                    self._dispatcher.set_pending_approval_id(run.pending_approval_id)
                run.requeue_suspended_nodes()
            logger.info(
                "[Plan] resuming run=%s — %d node(s), v%d",
                rid,
                len(plan_a.nodes),
                plan_a.version,
            )
            return run, plan_a

        await self._log.rebaseline_checkpoint(run)

        plan: Plan | None = None
        if self._initial_plan is not None:
            # A preset workflow plan is a reusable template — derive this
            # run's identity onto it. No copying needed: the blueprint is
            # frozen and never carries per-run state.
            plan = self._initial_plan.derive(plan_id=rid, task_input=task)
            await self._log.record_plan_created(plan, run)
            await _fire(
                self._hooks,
                HookEvent.PLAN_READY,
                plan_id=plan.plan_id,
                version=plan.version,
                agent=self._agent_name,
                nodes=_nodes_to_hook_dict(plan.nodes.values()),
                run_id=run.run_id,
            )
            logger.info(
                "[Plan] using hand-written workflow plan=%s — %d node(s)", rid, len(plan.nodes)
            )
            self._check_budget(run)
            return run, plan

        plan = await self._generate_plan(task, rid, run)
        if plan is None:
            if not run.last_error:
                logger.warning("[Scheduler] Failed to parse plan JSON — no nodes to execute")
                run.fail("Failed to parse plan JSON — no nodes to execute")
        else:
            self._check_budget(run)
        return run, plan

    async def _generate_plan(self, task: str, rid: str, run: AgentRun) -> Plan | None:
        """Ask the planner LLM for a DAG draft, validate it through the IR,
        and — when the validator rejects it — feed the issues back for one
        repair round (errors are feedback, not exceptions). The raw model
        text is kept on the transcript: the draft is auditable evidence of
        what the plan was derived from, not just the parsed result."""
        assert self._planner is not None and self._log is not None  # plan mode wires both
        try:
            draft = await self._planner.generate(task, self._system, list(run.messages), run)
            if draft.nodes:
                draft = await self._validated(draft, task, run)
        except LLMError as exc:
            logger.error("[Scheduler] planning LLM call failed: %s", exc)
            run.fail(exc)
            return None
        if not draft.nodes:
            return None
        plan = compile_planned(draft.nodes).derive(plan_id=rid, task_input=task)
        run.messages.append({"role": "assistant", "content": draft.raw_text})
        await self._log.record_plan_created(plan, run)
        await _fire(
            self._hooks,
            HookEvent.PLAN_READY,
            plan_id=plan.plan_id,
            version=plan.version,
            agent=self._agent_name,
            nodes=_nodes_to_hook_dict(plan.nodes.values()),
            run_id=run.run_id,
        )
        return plan

    async def _validated(self, draft: PlanDraft, task: str, run: AgentRun) -> PlanDraft:
        """Every model draft revalidates; a rejected draft gets exactly one
        repair round with the issues quoted back, then the verdict stands."""
        try:
            compile_planned(draft.nodes)
            return draft
        except PlanValidationError as exc:
            logger.warning("[Scheduler] planner draft rejected:\n%s", exc)
            assert self._planner is not None
            repaired = await self._planner.repair(draft, str(exc), task, self._system, run)
            compile_planned(repaired.nodes)  # second verdict is final
            return repaired

    async def gate(self, plan: Plan, run: AgentRun) -> Plan | None:
        """The plan-level HITL approval gate — "may this plan run at all?"

        Lives in the bootstrap (not the execution loop) because whether a
        plan may start is an opening question. Three outcomes: approved
        (plan returned), awaiting review (run SUSPENDED, snapshot saved so
        resume re-enters here), rejected (run fails — the reviewer's reason
        is the failure)."""
        if self._hooks is None:
            return plan
        assert self._log is not None  # gate runs in plan mode only
        try:
            veto = await self._hooks.check_blocking(
                Gate.PLAN_APPROVAL,
                plan_id=plan.plan_id,
                version=plan.version,
                agent=self._agent_name,
                nodes=_nodes_to_hook_dict(plan.nodes.values(), include_terminal=True),
                run_id=run.run_id,
                pending_approval_id=run.pending_approval_id,
            )
        except SuspendPendingApproval as exc:
            run.suspend(f"plan suspended pending approval: {exc}")
            run.pending_approval_id = exc.request_id
            await self._log.save_snapshot(run, plan=plan)
            logger.info(
                "[Plan] plan=%s SUSPENDED for HITL review (request_id=%s)",
                plan.plan_id,
                exc.request_id,
            )
            return None
        if veto.blocked:
            run.fail(veto.reason or "plan rejected by HITL reviewer")
            run.pending_approval_id = None
            logger.info("[Plan] plan=%s REJECTED by HITL — run fails", plan.plan_id)
            return None
        run.pending_approval_id = None
        return plan

    async def _resolve_reactive_run(
        self,
        task: str,
        run_id: str,
        *,
        parent_run_id: str | None = None,
    ) -> AgentRun:
        """Where a reactive run comes from: seeded messages (chat turn) →
        checkpoint resume (dangling tool turns pruned, crash scene cleared) →
        fresh. Order matters — a chat turn never accidentally resumes a
        checkpoint."""
        from prodagent.kernel.types import Message, RunState

        if self._initial_messages is not None:
            run = AgentRun(run_id=run_id, task=task, parent_run_id=parent_run_id, depth=self._depth)
            run.messages = list(self._initial_messages)
            logger.info("[Scheduler] chat turn: %d seeded messages", len(run.messages))
            return run

        if self._checkpoint_store is not None and run_id:
            existing = await self._checkpoint_store.load(run_id)
            if existing is not None:
                if existing.state is not RunState.SUSPENDED:
                    _prune_unresolved_tool_uses(existing)
                existing.revive()
                logger.info(
                    "[Scheduler] resuming from checkpoint: %d messages, turn=%d",
                    len(existing.messages),
                    existing.turn_count,
                )
                return existing

        run = AgentRun(run_id=run_id, task=task, parent_run_id=parent_run_id, depth=self._depth)
        run.messages.append(Message(role="user", content=task))
        return run
