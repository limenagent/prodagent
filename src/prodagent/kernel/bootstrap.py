"""PlanBootstrap — run/plan preparation, resumption, and the HITL approval gate.

Where a run comes from, in source order: a resumable event-log state (crash
recovery), a preset Workflow plan, the injected planner — or a single unit
wrapped as a one-node graph (the agent-as-unit shape, with its own
checkpoint-based resume). Keeping the choice here means the scheduler starts
every run the same way: with a (run, plan) pair in hand. No modes — the
shape of the first graph is a composition decision, not an enum.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from prodagent.base.determinism import new_uuid4
from prodagent.base.errors import SuspendPendingApproval
from prodagent.kernel.bus import Gate, HookEvent
from prodagent.kernel.bus import fire as _fire
from prodagent.kernel.graph import Origin, Plan
from prodagent.kernel.run import Run

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable

    from prodagent.kernel.body import NodeBody
    from prodagent.kernel.bus import HookRegistry
    from prodagent.kernel.event_log import PlanEventLog
    from prodagent.kernel.graph import Node
    from prodagent.kernel.types import MessageList
    from prodagent.ports import CheckpointStore

logger = logging.getLogger(__name__)


def _prune_unresolved_tool_uses(run: Run) -> None:
    # A crash mid-batch leaves an assistant round whose tool call never got
    # a result; providers reject a replayed tool_use without its
    # tool_result, so the dangling round is dropped and re-thought.
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


def single_body_plan(unit: NodeBody, *, plan_id: str) -> Plan:
    """One unit, one terminal node, no edges — the agent-as-unit shape.
    There is no special name for this graph and no mode behind it: running
    an agent IS running a (very small) graph."""
    from prodagent.kernel.graph import Node

    plan = Plan(plan_id=plan_id, origin=Origin.STATIC)
    plan.add_nodes([Node(node_id="unit", body=unit, is_terminal=True, origin=Origin.STATIC)])
    return plan


class PlanBootstrap:
    """Resolves the initial (run, plan) pair and gates it through HITL approval."""

    def __init__(
        self,
        log: PlanEventLog | None,
        *,
        system: str = "",
        initial_messages: MessageList | None = None,
        hooks: HookRegistry | None = None,
        agent_name: str = "",
        initial_plan: Plan | None = None,
        initial_body: NodeBody | None = None,
        dispatcher: Any | None = None,
        check_budget: Callable[[Run], None],
        checkpoint_store: CheckpointStore | None = None,
        depth: int = 0,
    ) -> None:
        self._log = log
        self._system = system
        self._initial_messages = list(initial_messages) if initial_messages else None
        self._hooks = hooks
        self._agent_name = agent_name
        self._initial_plan = initial_plan
        self._initial_body = initial_body
        self._dispatcher = dispatcher
        self._check_budget = check_budget
        self._checkpoint_store = checkpoint_store
        self._depth = depth

    async def prepare(
        self, task: str, run_id: str | None, *, parent_run_id: str | None = None
    ) -> tuple[Run, Plan | None]:
        """Resolve the initial (run, plan) pair — new or resumed, caller never
        cares which.

        Sources in priority order: a single unit wrapped as a one-node graph
        (the agent-as-unit shape, with its own resume path), a resumable
        event-log state (crash recovery), a preset Workflow plan, and the
        injected planner. Keeping the choice here means the executor starts
        every run the same way: with a (run, plan) pair in hand."""
        rid = run_id or new_uuid4()

        if self._initial_body is not None:
            run = await self._resolve_body_run(task, rid, parent_run_id=parent_run_id)
            return run, single_body_plan(self._initial_body, plan_id=rid)

        run = Run(run_id=rid, task=task, parent_run_id=parent_run_id, depth=self._depth)
        assert self._log is not None  # graph tracking always wires a PlanEventLog
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

        # No drafting source: the framework does not ask a model for an
        # execution graph (column 24 — a model's plan is task-list DATA; the
        # graph is code). A graph arrives as a preset plan; everything else
        # runs as a single body.
        raise ValueError(
            "no plan source: preset a Plan (Agent(workflow=...), "
            "compile_planned(...)) or run a body (initial_body) — "
            "the framework no longer drafts graphs from tasks"
        )

    async def gate(self, plan: Plan, run: Run) -> Plan | None:
        """The plan-level HITL approval gate — "may this plan run at all?"

        Lives in the bootstrap (not the execution loop) because whether a
        plan may start is an opening question. Three outcomes: approved
        (plan returned), awaiting review (run SUSPENDED, snapshot saved so
        resume re-enters here), rejected (run fails — the reviewer's reason
        is the failure)."""
        if self._hooks is None:
            return plan
        assert self._log is not None  # gate runs with graph tracking only
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
            await self._log.record_command_denied(
                plan,
                run,
                command="plan_approval",
                reason=veto.reason or "plan rejected by HITL reviewer",
            )
            run.fail(veto.reason or "plan rejected by HITL reviewer")
            run.pending_approval_id = None
            logger.info("[Plan] plan=%s REJECTED by HITL — run fails", plan.plan_id)
            return None
        run.pending_approval_id = None
        return plan

    async def _resolve_body_run(
        self,
        task: str,
        run_id: str,
        *,
        parent_run_id: str | None = None,
    ) -> Run:
        """Where a single-unit run comes from: seeded messages (a chat turn)
        → checkpoint resume (dangling tool rounds pruned, crash scene
        cleared) → fresh. Order matters — a chat turn never accidentally
        resumes a checkpoint."""
        from prodagent.kernel.types import Message, RunState

        if self._initial_messages is not None:
            run = Run(run_id=run_id, task=task, parent_run_id=parent_run_id, depth=self._depth)
            run.messages = list(self._initial_messages)
            logger.info("[Scheduler] chat turn: %d seeded messages", len(run.messages))
            return run

        if self._checkpoint_store is not None and run_id:
            existing = await self._checkpoint_store.load(run_id)
            if existing is not None:
                if existing.state is not RunState.SUSPENDED:
                    _prune_unresolved_tool_uses(existing)
                existing.resume()
                logger.info(
                    "[Scheduler] resuming from checkpoint: %d messages, turn=%d",
                    len(existing.messages),
                    existing.turn_count,
                )
                return existing

        run = Run(run_id=run_id, task=task, parent_run_id=parent_run_id, depth=self._depth)
        run.messages.append(Message(role="user", content=task))
        return run
