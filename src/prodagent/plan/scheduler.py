"""Scheduler — the one execution engine.

It does exactly one thing, over and over: compute the ready set, run it as
one wave, apply the outcome — until nothing is ready. Where the plan came
from (an LLM, a hand-written Workflow, or REACTIVE's degenerate single-node
blueprint) never reaches this loop; ``bootstrap`` hands over a (run, plan)
pair and the scheduler just schedules. That is the whole point of the
degenerate unification (column 4): one engine, so persistence, recovery,
approval, observation and replay are implemented once and shared by every
mode.

Wave discipline: readonly bodies run concurrently (bounded), write bodies
one at a time — two HIGH side-effect calls must never race just because
the DAG unblocked them together. A suspension or handoff stops the wave:
the run is already waiting on a human or a peer, firing more side effects
would be wrong. Failures don't stop it — a failed write lets its siblings
run, and only the primary failure triggers a replan.

Run-death exceptions (budget exhausted, dead-loop detection) are not node
failures — they float out of the waves and settle the run here. The empty
ready set with unfinished, un-suspended nodes is the cycle/deadlock
signal; a wave loop over an LLM-authored graph is bounded by node count ×
replans, never a bare while-True.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from prodagent.base.config import ContextConfig, LoopConfig
from prodagent.base.determinism import value_override
from prodagent.base.errors import BudgetExceeded, InfiniteLoopDetected, LLMError
from prodagent.base.event_log import RunEventType
from prodagent.base.run_context import run_scope
from prodagent.base.time_recorder import RecordingTimePort
from prodagent.base.types import ExecutionMode
from prodagent.hooks import fire as _fire
from prodagent.kernel.bodies.base import ReActBody
from prodagent.kernel.bodies.runner import BodyRunner
from prodagent.kernel.budget import check_spawn_budget
from prodagent.kernel.bus import HookEvent, save_and_fire_checkpoint
from prodagent.kernel.react import ReactEngine
from prodagent.kernel.types import (
    AgentEvent,
    MessageList,
    NodeCompletedEvent,
    NodeFailedEvent,
    NodeStartedEvent,
    NodeStatus,
    RunFailedEvent,
    RunState,
)
from prodagent.plan.bootstrap import PlanBootstrap
from prodagent.plan.command import REDUCERS, Command, Goto, Send, Update
from prodagent.plan.dag import Node, Origin, Plan
from prodagent.plan.event_log import PlanEventLog
from prodagent.plan.finalize import finalize_run, terminal_event
from prodagent.plan.node_runner import (
    NodeFailed,
    NodeHandoff,
    NodeOutcome,
    NodeRunner,
    NodeSuccess,
    NodeSuspended,
    commit_transcript,
)
from prodagent.plan.planner import Planner

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator, Coroutine, Iterator

    from prodagent.cognition.context.manager import ContextManager
    from prodagent.cognition.context.spill import ToolResultSpillStore
    from prodagent.kernel.budget import BudgetLedger, HardBudget
    from prodagent.kernel.bus import HookRegistry
    from prodagent.kernel.state import AgentRun
    from prodagent.llm import LLMClient
    from prodagent.ports import CheckpointStore, EventLog
    from prodagent.tooling.dispatcher import ToolDispatcher

logger = logging.getLogger(__name__)

_MAX_ITERS_PER_NODE = 3
_MAX_ITERS_SLOP = 5

__all__ = ["Scheduler", "reactive_scheduler"]


@dataclass(slots=True)
class _BatchResult:
    successes: list[NodeSuccess] = field(default_factory=list)
    failures: list[NodeFailed] = field(default_factory=list)
    suspended: NodeSuspended | None = None
    handoff: NodeHandoff | None = None


class Scheduler:
    """Drives a (run, plan) pair to a terminal state — the only engine."""

    def __init__(
        self,
        llm: LLMClient,
        bodies: BodyRunner,
        *,
        mode: ExecutionMode = ExecutionMode.PLAN_FIRST,
        system: str = "",
        initial_messages: MessageList | None = None,
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
        react: ReactEngine | None = None,
        depth: int = 0,
    ) -> None:
        self._system = system
        self._budget = budget
        self._budget_ledger = budget_ledger
        self._hooks = hooks
        self._dispatcher = dispatcher
        self._max_replans = max_replans
        self._mode = mode
        self._react = react
        self._replan_count = 0

        log: PlanEventLog | None = None
        if mode is ExecutionMode.PLAN_FIRST:
            # PLAN_FIRST's DAG state is only ever tracked in these two stores
            # (unlike REACTIVE, which treats checkpointing as opt-in) — bare
            # profile still needs a working pair.
            if event_log is None:
                from prodagent.backends.factory import in_memory_event_log

                event_log = in_memory_event_log()
            if checkpoint_store is None:
                from prodagent.backends.factory import in_memory_checkpoint_store

                checkpoint_store = in_memory_checkpoint_store()
            assert event_log is not None and checkpoint_store is not None
            log = PlanEventLog(event_log=event_log, checkpoint_store=checkpoint_store, hooks=hooks)
        self._event_log = event_log
        self._checkpoint_store = checkpoint_store
        self._log = log
        self._planner: Planner | None = (
            Planner(
                llm=llm,
                config=None,
                tool_schemas=tool_schemas or [],
                hooks=hooks,
                framework_config=framework_config,
            )
            if mode is ExecutionMode.PLAN_FIRST
            else None
        )
        self._node_runner = NodeRunner(
            bodies,
            self._log,
            hooks=hooks,
            agent_name=agent_name,
            dispatcher=dispatcher,
        )
        self._bootstrap = PlanBootstrap(
            self._log,
            self._planner,
            mode=mode,
            system=system,
            initial_messages=initial_messages,
            hooks=hooks,
            agent_name=agent_name,
            initial_plan=initial_plan,
            dispatcher=dispatcher,
            check_budget=self._check_budget,
            checkpoint_store=checkpoint_store if mode is ExecutionMode.REACTIVE else None,
            depth=depth,
        )
        limit = getattr(getattr(framework_config, "loop", None), "readonly_concurrency", None)
        self._readonly_gate = asyncio.Semaphore(limit) if limit else None
        self._unlocked: set[str] = set()
        """Dynamic edges lit by Gotos this stream — deps satisfied by decree."""

    def _check_budget(self, run: AgentRun) -> None:
        check_spawn_budget(run, self._budget, self._budget_ledger)

    # ── The run lifecycle ───────────────────────────────────────────────────

    async def stream(
        self,
        task: str,
        *,
        run_id: str | None = None,
        parent_run_id: str | None = None,
    ) -> AsyncGenerator[AgentEvent, None]:
        """The whole lifecycle in four moves: prepare (new or resumed) →
        HITL gate (plan-sourced graphs) → waves → finalize. Whatever happens
        inside — suspend, handoff, budget death — the terminal event at the
        end is guaranteed: no stream ends without one (an unexpected crash
        settles the run and re-raises; the driver synthesizes the event)."""
        run, plan = await self._bootstrap.prepare(task, run_id, parent_run_id=parent_run_id)
        clock = RecordingTimePort() if self._event_log is not None else None
        if self._react is not None:
            self._react.bind_clock(clock)
        with run_scope(run.run_id, self._event_log), value_override(time_port=clock):
            logger.info("Scheduler[%s] stream started: %r", run.run_id, task[:80])
            await _fire(self._hooks, HookEvent.LOOP_START, run_id=run.run_id, task=task[:200])
            try:
                if plan is not None and self._mode is ExecutionMode.PLAN_FIRST:
                    plan = await self._bootstrap.gate(plan, run)
                if plan is not None:
                    async for event in self._waves(plan, run):
                        yield event
            except BudgetExceeded as exc:
                yield await self._settle_terminated(run, exc)
            except InfiniteLoopDetected as exc:
                yield await self._settle_terminated(run, exc)
            except Exception as exc:
                await self._settle_unexpected(run, exc)
                raise
            else:
                await _fire(self._hooks, HookEvent.LOOP_END, run_id=run.run_id, error=None)
                await self._record_terminal_marker(run)
            finally:
                finalize_run(run, plan)
                if self._mode is ExecutionMode.REACTIVE and self._checkpoint_store is not None:
                    await save_and_fire_checkpoint(self._checkpoint_store, run, self._hooks)
        yield terminal_event(run)

    async def _settle_terminated(
        self, run: AgentRun, exc: BudgetExceeded | InfiniteLoopDetected
    ) -> AgentEvent:
        run.fail(exc)
        await _fire(self._hooks, HookEvent.LOOP_END, run_id=run.run_id, error=str(exc))
        await self._record_terminal_marker(run)
        logger.warning("Scheduler[%s] terminated: %s", run.run_id, exc)
        return RunFailedEvent(run=run, error=str(exc))

    async def _settle_unexpected(self, run: AgentRun, exc: BaseException) -> None:
        run.fail(exc)
        await _fire(self._hooks, HookEvent.LOOP_END, run_id=run.run_id, error=str(exc))
        await self._record_terminal_marker(run)
        logger.exception("Scheduler[%s] unexpected error", run.run_id)

    async def _record_terminal_marker(self, run: AgentRun) -> None:
        """Terminal marker on the run's stream — REACTIVE's replay tail (the
        tape catalog reads the last Run* marker as the terminal state)."""
        if self._mode is not ExecutionMode.REACTIVE or self._react is None:
            return
        event_type = (
            RunEventType.RUN_SUSPENDED
            if run.state is RunState.SUSPENDED
            else RunEventType.RUN_FAILED
            if run.state is RunState.FAILED
            else RunEventType.RUN_COMPLETED
        )
        await self._react.record_terminal(run, event_type)

    # ── The wave loop ───────────────────────────────────────────────────────

    async def _waves(self, plan: Plan, run: AgentRun) -> AsyncGenerator[AgentEvent, None]:
        """Each pass dispatches every dependency-satisfied node (concurrently),
        then handles failures — possibly via replan, which folds replacement
        nodes into the next plan version for the next pass.

        No while-True over an LLM-authored graph: node count × replans
        bounds the loop, with a little slop for replan churn."""
        max_iterations = len(plan.nodes) * _MAX_ITERS_PER_NODE + _MAX_ITERS_SLOP

        for _ in range(max_iterations):
            if plan.is_complete(run.node_states):
                return
            self._check_budget(run)
            ready = plan.ready(run.node_states, frozenset(self._unlocked))
            if not ready:
                # Ready-empty with unfinished, un-suspended nodes is the
                # cycle/deadlock signal; a properly-formed plan should never
                # get here (acyclicity is asserted at construction).
                return

            for n in ready:
                yield NodeStartedEvent(node_id=n.node_id, action=n.action, run_id=run.run_id)

            wave_outcomes: list[list[NodeOutcome]] = []
            async for event in self._dispatch_wave(ready, plan, run, wave_outcomes):
                yield event
            outcomes = wave_outcomes[0] if wave_outcomes else []
            batch = self._classify_outcomes(outcomes)

            for event in self._emit_node_events(batch, run):
                yield event

            commands = [(succ.node, c) for succ in batch.successes for c in succ.commands]
            if commands:
                for applied_event in await self._apply_commands(commands, plan, run):
                    yield applied_event

            if batch.handoff is not None:
                return
            if batch.suspended is not None:
                return
            # Post-wave budget check — but a run that just reached a terminal
            # state is done, not over budget (the next Turn would have been
            # the one to refuse).
            if run.state is RunState.RUNNING:
                self._check_budget(run)

            if batch.failures:
                exhausted, plan = await self._handle_failures(batch.failures, plan, run)
                if exhausted:
                    return

    async def _dispatch_wave(
        self,
        ready: list[Node],
        plan: Plan,
        run: AgentRun,
        outcomes_box: list[list[NodeOutcome]],
    ) -> AsyncGenerator[AgentEvent, None]:
        """Dispatch ready nodes with the ordering discipline of column 4's
        waves: readonly bodies run concurrently (bounded), write bodies one
        at a time. A write node streams its live events (a ReActBody's
        Turns) straight into the run's stream as it executes."""
        by_node: dict[str, NodeOutcome] = {}

        readonly = [n for n in ready if self._node_is_readonly(n)]
        writes = [n for n in ready if n.node_id not in {r.node_id for r in readonly}]

        if readonly:
            tasks = [self._node_runner.run_one(n, plan, run) for n in readonly]
            if self._readonly_gate is not None:
                gate = self._readonly_gate

                async def _bounded(coro: Coroutine[Any, Any, NodeOutcome]) -> NodeOutcome:
                    async with gate:
                        return await coro

                tasks = [_bounded(t) for t in tasks]
            raw = await asyncio.gather(*tasks, return_exceptions=True)
            for node, r in zip(readonly, raw, strict=True):
                if isinstance(r, asyncio.CancelledError):
                    raise r
                if isinstance(r, (BudgetExceeded, InfiniteLoopDetected)):
                    raise r
                if isinstance(r, BaseException):
                    by_node[node.node_id] = NodeFailed(node=node, error=r)
                else:
                    by_node[node.node_id] = r

        for node in writes:
            if run.state is not RunState.RUNNING or run.pending_handoff is not None:
                break
            outcome: list[NodeOutcome] = []
            try:
                async for event in self._node_runner.stream_one(node, plan, run, outcome):
                    yield event
            except (
                asyncio.CancelledError,
                BudgetExceeded,
                InfiniteLoopDetected,
            ):
                raise
            except BaseException as exc:  # noqa: BLE001 — a node failure is data, not a crash
                if isinstance(node.body, ReActBody):
                    # A react loop's crash already floated through the node
                    # runner as a raise — it is the run's crash, not data.
                    raise
                by_node[node.node_id] = NodeFailed(node=node, error=exc)
                continue
            by_node[node.node_id] = outcome[0]

        # Nodes never launched (wave stopped by suspend/handoff) are omitted —
        # they stay PENDING in the states and re-run after resume.
        outcomes = [by_node[n.node_id] for n in ready if n.node_id in by_node]
        for oc in outcomes:
            if isinstance(oc, NodeSuccess):
                commit_transcript(oc.node, oc, run)
        outcomes_box.append(outcomes)

    async def _apply_commands(
        self,
        commands: list[tuple[Node, Command]],
        plan: Plan,
        run: AgentRun,
    ) -> list[AgentEvent]:
        """Apply the wave's dynamic control flow: every command passes its
        runtime gate, lands in the event log, and changes what runs next.
        Goto lights a node up (reset on redo), Send sprouts instances and
        re-wires downstream (the join is the barrier), Update merges into
        the run's shared state under a declared reducer."""
        events: list[AgentEvent] = []
        for node, command in commands:
            if isinstance(command, Goto):
                if plan.get_node(command.target) is None:
                    raise ValueError(
                        f"Goto from {node.node_id!r}: target {command.target!r} is not in the plan"
                    )
                state = run.node_state(command.target)
                if state.status is not NodeStatus.PENDING:
                    state.reset_to_pending()
                self._unlocked.add(command.target)
                # The sender re-enters too: a goto is the sender asking for
                # another round (loop = target redo + sender redo). Orbit
                # limits are policy — the wave bound and dead-loop guard.
                run.node_state(node.node_id).reset_to_pending()
                logger.info("[Plan] goto %s → %s (sender re-enters)", node.node_id, command.target)

            elif isinstance(command, Send):
                template = plan.get_node(command.template)
                if template is None:
                    raise ValueError(
                        f"Send from {node.node_id!r}: template {command.template!r} "
                        "is not in the plan"
                    )
                instances = [
                    Node(
                        node_id=inst_id,
                        body=template.body,
                        params={**dict(template.params), "item": item},
                        depends_on=(node.node_id,),
                        is_terminal=False,
                        origin=Origin.DYNAMIC,
                    )
                    for inst_id, item in zip(command.instance_ids(), command.items, strict=True)
                ]
                # The template is a seed, not a worker: if it has not run
                # yet, the instances replace it (a completed template stays
                # — its output is real and the fan-out reuses its body).
                template_state = run.node_state(command.template)
                if template_state.status is NodeStatus.PENDING:
                    template_state.mark_obsolete()
                plan.sprout(instances, rewire_from=node.node_id)
                logger.info(
                    "[Plan] send %s → %d instance(s) of %r",
                    node.node_id,
                    len(instances),
                    command.template,
                )

            elif isinstance(command, Update):
                reducer = REDUCERS.get(command.reducer or "")
                if command.reducer is not None and reducer is None:
                    raise ValueError(
                        f"Update from {node.node_id!r}: unknown reducer "
                        f"{command.reducer!r} (declared: {sorted(REDUCERS)})"
                    )
                if command.key in run.shared and reducer is None:
                    raise ValueError(
                        f"Update from {node.node_id!r}: key {command.key!r} already "
                        "written and no reducer declared — two nodes merging one "
                        "key must say how"
                    )
                if command.key in run.shared:
                    assert reducer is not None  # the gate above demanded one
                    run.shared[command.key] = reducer(run.shared[command.key], command.value)
                else:
                    run.shared[command.key] = command.value
                logger.info(
                    "[Plan] update %s: %s = %r", node.node_id, command.key, run.shared[command.key]
                )

            if self._log is not None:
                await self._log.record_command_applied(plan, run, node.node_id, command)

        return events

    def _node_is_readonly(self, node: Node) -> bool:
        # Bodies know their own side-effect class; only ToolBody defers to the
        # registry's metadata. No metadata → treat as a write (serial, safe).
        if node.body.readonly is not None:
            return node.body.readonly
        if self._dispatcher is None:
            return False
        meta = self._dispatcher.meta_for(node.body.target)
        return meta.is_readonly if meta is not None else False

    @staticmethod
    def _classify_outcomes(outcomes: list[NodeOutcome]) -> _BatchResult:
        """Fold a wave into one summary. Successes and failures accumulate;
        suspend/handoff keep only the first — the wave stops at either, so
        a second one cannot logically exist."""

        batch = _BatchResult()
        for oc in outcomes:
            match oc:
                case NodeSuccess():
                    batch.successes.append(oc)
                case NodeFailed():
                    batch.failures.append(oc)
                case NodeSuspended() if batch.suspended is None:
                    batch.suspended = oc
                case NodeHandoff() if batch.handoff is None:
                    batch.handoff = oc
        return batch

    @staticmethod
    def _emit_node_events(batch: _BatchResult, run: AgentRun) -> Iterator[AgentEvent]:
        """Translate one wave into stream events. On suspend/handoff only
        the successes that did land are reported — the run is pausing, and
        un-launched nodes keep their PENDING status for the resume."""
        for succ in batch.successes:
            yield NodeCompletedEvent(
                node_id=succ.node.node_id,
                action=succ.node.action,
                result=run.node_state(succ.node.node_id).output_ref,
                run_id=run.run_id,
            )
        if batch.handoff is not None or batch.suspended is not None:
            return
        for fail in batch.failures:
            yield NodeFailedEvent(
                node_id=fail.node.node_id,
                action=fail.node.action,
                error=str(fail.error),
                run_id=run.run_id,
            )

    async def _handle_failures(
        self,
        failures: list[NodeFailed],
        plan: Plan,
        run: AgentRun,
    ) -> tuple[bool, Plan]:
        """Only the *primary* failure gets a replan — one LLM replan per wave.
        Secondary failures are recorded as-is: the replan will see the whole
        failed neighbourhood anyway, and parallel replans would race each
        other into conflicting merges."""
        primary, *secondary = failures
        for fail in secondary:
            await self._record_failure(fail, plan, run, replan=False)
        return await self._record_failure(primary, plan, run)

    async def _record_failure(
        self,
        failure: NodeFailed,
        plan: Plan,
        run: AgentRun,
        *,
        replan: bool = True,
    ) -> tuple[bool, Plan]:
        """Incremental replanning — PLAN_FIRST's most valuable property.

        On failure the doomed downstream is obsoleted (never deleted), the
        planner is handed the failure and proposes *replacement* nodes, and
        the merge keeps completed nodes untouched: they may carry side
        effects that must not re-fire. Returns ``(stop, plan)`` — stop is
        True (end the run) when replanning is exhausted, disabled, or the
        planner itself failed; plan is the (possibly new) version to
        continue with."""
        node = failure.node
        exc = failure.error
        run.node_state(node.node_id).mark_failed(str(exc))
        run.tool_failures += 1
        obsoleted = plan.mark_downstream_obsolete(node.node_id, run.node_states)
        assert self._log is not None  # failures only happen in plan mode
        await self._log.record_node_failed(plan, run, node.node_id, str(exc))
        await _fire(
            self._hooks,
            HookEvent.NODE_FAILED,
            plan_id=run.run_id,
            node_id=node.node_id,
            action=node.action,
            error=str(exc),
            run_id=run.run_id,
        )
        logger.error("[Plan] node=%s FAILED: %s  obsoleted=%s", node.node_id, exc, obsoleted)

        if not replan:
            return False, plan
        if self._replan_count >= self._max_replans:
            logger.error("[Plan] max_replans=%d reached — stopping", self._max_replans)
            return True, plan

        assert self._planner is not None
        try:
            new_nodes = await self._planner.replan(
                plan, node, str(exc), self._system, run.messages, run
            )
        except LLMError as replan_exc:
            logger.error("[Plan] replan LLM call failed: %s", replan_exc)
            run.fail(replan_exc)
            return True, plan
        if not new_nodes:
            logger.warning("[Plan] replan #%d produced no nodes — stopping", self._replan_count + 1)
            return True, plan

        plan = plan.merge(new_nodes, run.node_states)
        self._replan_count += 1
        await self._log.record_replanned(plan, run, new_nodes)
        await _fire(
            self._hooks,
            HookEvent.PLAN_REPLANNED,
            plan_id=plan.plan_id,
            version=plan.version,
            failed_node=node.node_id,
            new_nodes=[
                {
                    "id": n.node_id,
                    "action": n.action,
                    "params": dict(n.params),
                    "depends_on": list(n.depends_on),
                }
                for n in new_nodes
            ],
            replan_count=self._replan_count,
            run_id=run.run_id,
        )
        logger.info(
            "[Plan] replan #%d → V%d  new_nodes=%s",
            self._replan_count,
            plan.version,
            [n.node_id for n in new_nodes],
        )
        return False, plan


def reactive_scheduler(
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
    """REACTIVE as a Scheduler: the Turn loop as a body, the body as a
    single-node plan — the degenerate unification, assembled and ready to
    drive. The composition root for standalone REACTIVE runs (tests, the
    replay deck); the factory builds the same shape per hop."""
    engine = ReactEngine(
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
        llm,
        BodyRunner(dispatcher.dispatch, react=engine),
        mode=ExecutionMode.REACTIVE,
        system=system_prompt,
        initial_messages=initial_messages,
        hooks=hooks,
        budget=budget,
        event_log=event_log,
        checkpoint_store=checkpoint_store,
        budget_ledger=budget_ledger,
        react=engine,
    )
