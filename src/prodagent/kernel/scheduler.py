"""Scheduler — the one execution engine.

It does exactly one thing, over and over: compute the ready set, run it as
one wave, apply the outcome — until nothing is ready. Where the graph came
from (a hand-written Workflow, a preset plan, a resumed checkpoint, a
single body wrapped as one node) never reaches this loop; ``bootstrap``
hands over a (run, plan) pair and the scheduler just schedules — one
engine, so persistence, recovery, approval, observation and replay are
implemented once and shared by every shape.

Wave discipline: readonly bodies run concurrently (bounded), write bodies
one at a time — two HIGH side-effect calls must never race just because
the DAG unblocked them together. A suspension or handoff stops the wave:
the run is already waiting on a human or a peer, firing more side effects
would be wrong. Failures don't stop the wave — a failed write lets its siblings run —
but the run ends failed once the wave classifies.

Run-death exceptions (budget exhausted, dead-loop detection) are not node
failures — they float out of the waves and settle the run here. The empty
ready set with unfinished, un-suspended nodes is the cycle/deadlock
signal; the wave loop is bounded by the wave cap, never a bare
while-True.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, cast

from prodagent.base.determinism import value_override
from prodagent.base.errors import BudgetExceeded, InfiniteLoopDetected, Stalled
from prodagent.base.event_log import RunEventType
from prodagent.base.run_context import run_scope
from prodagent.base.time_recorder import RecordingTimePort
from prodagent.kernel.bootstrap import PlanBootstrap
from prodagent.kernel.budget import check_spawn_budget
from prodagent.kernel.bus import HookEvent, save_and_fire_checkpoint
from prodagent.kernel.bus import fire as _fire
from prodagent.kernel.channels import AmbiguousWrite, WaveWrites, apply_channel_inits
from prodagent.kernel.command import REDUCERS, WAIT, Command, Goto, Send, Update
from prodagent.kernel.event_log import PlanEventLog
from prodagent.kernel.finalize import finalize_run, terminal_event
from prodagent.kernel.node_runner import (
    NodeFailed,
    NodeHandoff,
    NodeOutcome,
    NodeRunner,
    NodeSuccess,
    NodeSuspended,
    commit_transcript,
)
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

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator, Callable, Iterator, Mapping

    from prodagent.kernel.body import (
        LLMInvoker,
        NodeBody,
        SubagentInvoker,
        ToolExecutor,
    )
    from prodagent.kernel.budget import BudgetLedger, HardBudget
    from prodagent.kernel.bus import HookRegistry
    from prodagent.kernel.graph import Node, Plan
    from prodagent.kernel.run import Run
    from prodagent.ports import CheckpointStore, EventLog
    from prodagent.tooling.dispatcher import ToolDispatcher

logger = logging.getLogger(__name__)

_MAX_ITERS_PER_NODE = 3  # legacy formula kept for reference; waves cap is max_waves
_MAX_ITERS_SLOP = 5
_MAX_NO_PROGRESS_WAVES = 4
"""The no-progress threshold: this many executed waves with zero
shared-state change is a cycle whose body never writes what its exit
condition reads."""

__all__ = ["Scheduler"]


class _BareEventLog:
    """The kernel's own in-process event log — the graph executor's bare-profile
    default (state dies with the process; durability arrives by injecting
    a real backend). Same contract as the durable in-memory pair:
    per-stream monotonic seq, optimistic tail check, BASE capabilities
    only (``subscribe`` replays what exists and stops — nothing wakes on
    append in the bare profile)."""

    def __init__(self) -> None:
        self._streams: dict[str, list[Any]] = {}

    async def append(self, event: Any, expected_seq: int | None = None) -> int:
        return (await self.append_events([event], expected_seq))[0]

    async def append_events(self, events: list[Any], expected_seq: int | None = None) -> list[int]:
        return await self._append_batch(list(events), expected_seq)

    async def _append_batch(self, events: list[Any], expected_seq: int | None) -> list[int]:
        if not events:
            return []
        stream_id = events[0].stream_id
        stream = self._streams.setdefault(stream_id, [])
        if expected_seq is not None and len(stream) != expected_seq:
            from prodagent.base.errors import VersionConflict

            raise VersionConflict(
                f"expected tail seq {expected_seq} for stream {stream_id}, "
                f"found {len(stream)} — concurrent writer won"
            )
        # Seq convention: the tail check counts events (0 = empty), and an
        # appended event's seq is its 1-based position — tail and count agree.
        seqs = []
        for event in events:
            stream.append(event)
            with contextlib.suppress(AttributeError):  # frozen event: seq kept by position
                event.seq = len(stream)
            seqs.append(len(stream))
        return seqs

    async def get_events(self, stream_id: str) -> list[Any]:
        return list(self._streams.get(stream_id, ()))

    async def get_after(self, stream_id: str, *, since_seq: int) -> list[Any]:
        out = []
        for i, e in enumerate(self._streams.get(stream_id, ())):
            seq = getattr(e, "seq", i + 1)
            if seq > since_seq:
                out.append(e)
        return out

    async def subscribe(self, stream_id: str) -> Any:
        for event in list(self._streams.get(stream_id, ())):
            yield event


class _BareCheckpointStore:
    """The kernel's own in-process checkpoint store — bare-profile default.
    One snapshot per run (latest wins), optimistic version check preserved:
    the discipline is the same, only the durability is missing."""

    def __init__(self) -> None:
        self._runs: dict[str, Any] = {}
        self._versions: dict[str, int] = {}

    async def save(self, run: Any, expected_version: int | None = None) -> None:
        from prodagent.base.errors import VersionConflict

        stored = self._versions.get(run.run_id, 0)
        if expected_version is not None and stored != expected_version:
            raise VersionConflict(
                f"checkpoint version mismatch for run={run.run_id}: "
                f"expected {expected_version}, stored {stored}"
            )
        self._runs[run.run_id] = run
        self._versions[run.run_id] = stored + 1
        run.checkpoint_version = stored + 1

    async def load(self, run_id: str, version: int | None = None) -> Any | None:
        run = self._runs.get(run_id)
        if run is not None and version is not None and self._versions.get(run_id) != version:
            return None  # bare keeps only the latest — an old version is absent
        return run

    async def list_run_ids(self) -> list[str]:
        return list(self._runs)


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
        *,
        system: str = "",
        initial_messages: MessageList | None = None,
        hooks: HookRegistry | None = None,
        agent_name: str = "",
        event_log: EventLog | None = None,
        checkpoint_store: CheckpointStore | None = None,
        framework_config: Any = None,
        budget: HardBudget | None = None,
        initial_plan: Plan | None = None,
        budget_ledger: BudgetLedger | None = None,
        dispatcher: ToolDispatcher | None = None,
        initial_body: NodeBody | None = None,
        depth: int = 0,
        fns: Mapping[str, Callable[..., Any]] | None = None,
        llm_invoker: LLMInvoker | None = None,
        subagent: SubagentInvoker | None = None,
        tools: ToolExecutor | None = None,
        max_waves: int = 2048,
        wave_timeout: float | None = None,
        wiring: Mapping[str, Any] | None = None,
        terminal_marker: Callable[[Run, Any], Any] | None = None,
    ) -> None:
        self._system = system
        self._budget = budget
        self._budget_ledger = budget_ledger
        self._hooks = hooks
        self._dispatcher = dispatcher
        self._max_waves = max_waves
        self._wave_timeout = wave_timeout
        self._single_unit = initial_body
        self._instance_seq = 0

        log: PlanEventLog | None = None
        if initial_body is None:
            # Graph tracking: a preset, drafted, or resumed plan always tracks
            # DAG state in these two stores — bare profile still needs a
            # working pair. The kernel implements its own in-process pair (the
            # same port-implementation precedent as the BudgetLedger): durable
            # backends arrive by injection.
            # The bare pair structurally satisfies each port; the cast keeps
            # the protocol check at the boundary instead of on every member.
            resolved_log = cast("EventLog", event_log if event_log is not None else _BareEventLog())
            resolved_store = cast(
                "CheckpointStore",
                checkpoint_store if checkpoint_store is not None else _BareCheckpointStore(),
            )
            log = PlanEventLog(event_log=resolved_log, checkpoint_store=resolved_store, hooks=hooks)
        self._event_log: EventLog | None = resolved_log if log is not None else event_log
        self._checkpoint_store: CheckpointStore | None = (
            resolved_store if log is not None else checkpoint_store
        )
        self._log = log
        # The composition root's per-execution service bag and terminal
        # marker: generic seams the kernel carries without reading. The
        # wiring bag reaches bodies through the NodeContext (the loop recipe
        # fetches its driver there); the terminal marker is invoked at every
        # stream end so an app-level executor can leave its own closing fact
        # on the run's stream. Neither knows what a "loop" is.
        self._wiring = dict(wiring) if wiring else {}
        self._terminal_marker = terminal_marker
        # Column 7's barrier discipline: writes to declared channels buffer
        # here during a wave and fold at its end — same-wave nodes read the
        # wave-start snapshot, and the fold's result never depends on
        # completion order.
        self._wave_writes = WaveWrites()
        self._node_runner = NodeRunner(
            self._log,
            hooks=hooks,
            agent_name=agent_name,
            dispatcher=dispatcher,
            # A bare executor (tests, embedders) slots straight in; otherwise
            # the throat is the dispatcher's dispatch — same five gates.
            tools=tools if tools is not None else (dispatcher.dispatch if dispatcher else None),
            fns=fns,
            llm=llm_invoker,
            subagent=subagent,
            wiring=self._wiring,
            wave_writes=self._wave_writes,
        )
        self._bootstrap = PlanBootstrap(
            self._log,
            system=system,
            initial_messages=initial_messages,
            hooks=hooks,
            agent_name=agent_name,
            initial_plan=initial_plan,
            initial_body=initial_body,
            dispatcher=dispatcher,
            check_budget=self._check_budget,
            checkpoint_store=checkpoint_store if initial_body is not None else None,
            depth=depth,
        )
        limit = getattr(getattr(framework_config, "loop", None), "readonly_concurrency", None)
        self._readonly_gate = asyncio.Semaphore(limit) if limit else None

    def _check_budget(self, run: Run) -> None:
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
        if plan is not None:
            # The blueprint declares the rules; the run carries the folded
            # values. setdefault keeps resumed values (apply_channel_inits
            # deep-copies inits — a mutable default must not leak across runs).
            self._wave_writes.channels = plan.channels
            apply_channel_inits(plan.channels, run.shared)
        clock = RecordingTimePort() if self._event_log is not None else None
        with run_scope(run.run_id, self._event_log), value_override(time_port=clock):
            logger.info("Scheduler[%s] stream started: %r", run.run_id, task[:80])
            await _fire(self._hooks, HookEvent.LOOP_START, run_id=run.run_id, task=task[:200])
            try:
                if plan is not None and self._single_unit is None:
                    plan = await self._bootstrap.gate(plan, run)
                if plan is not None:
                    async for event in self._waves(plan, run):
                        yield event
            except BudgetExceeded as exc:
                yield await self._settle_terminated(run, exc)
            except InfiniteLoopDetected as exc:
                yield await self._settle_terminated(run, exc)
            except Stalled as exc:
                yield await self._settle_terminated(run, exc)
            except TimeoutError as exc:
                # A wave-scope timeout (column 18: a timeout is a scope
                # cancellation) — the straggler's node is mid-flight, its
                # outcome unknowable; the run fails loudly, resume redoes.
                yield await self._settle_terminated(run, exc)
            except Exception as exc:
                await self._settle_unexpected(run, exc)
                raise
            else:
                await _fire(self._hooks, HookEvent.LOOP_END, run_id=run.run_id, error=None)
                await self._record_terminal_marker(run)
            finally:
                finalize_run(run, plan)
                if self._single_unit is not None and self._checkpoint_store is not None:
                    await save_and_fire_checkpoint(self._checkpoint_store, run, self._hooks)
        yield terminal_event(run)

    async def _settle_terminated(
        self, run: Run, exc: BudgetExceeded | InfiniteLoopDetected | Stalled | TimeoutError
    ) -> AgentEvent:
        run.fail(exc)
        await _fire(self._hooks, HookEvent.LOOP_END, run_id=run.run_id, error=str(exc))
        await self._record_terminal_marker(run)
        logger.warning("Scheduler[%s] terminated: %s", run.run_id, exc)
        return RunFailedEvent(run=run, error=str(exc))

    async def _settle_unexpected(self, run: Run, exc: BaseException) -> None:
        run.fail(exc)
        await _fire(self._hooks, HookEvent.LOOP_END, run_id=run.run_id, error=str(exc))
        await self._record_terminal_marker(run)
        logger.exception("Scheduler[%s] unexpected error", run.run_id)

    async def _record_terminal_marker(self, run: Run) -> None:
        """Terminal marker on the run's stream — the single-unit replay tail
        (the tape catalog reads the last Run* marker as the terminal state).

        The kernel says *when* (every stream end, single-body shape); the
        composition root's callback says *what* lands — the app-level
        executor's own closing fact, appended on its own cursor chain."""
        if self._single_unit is None or self._terminal_marker is None:
            return
        event_type = (
            RunEventType.RUN_SUSPENDED
            if run.state is RunState.SUSPENDED
            else RunEventType.RUN_FAILED
            if run.state is RunState.FAILED
            else RunEventType.RUN_COMPLETED
        )
        await self._terminal_marker(run, event_type)

    # ── The wave loop ───────────────────────────────────────────────────────

    async def _waves(self, plan: Plan, run: Run) -> AsyncGenerator[AgentEvent, None]:
        """Each pass dispatches every dependency-satisfied node (concurrently),
        applies the wave barrier (channel folds, requeues, goto), then handles
        failures — a failed node quarantines its downstream and fails the run
        (recovery is application composition, not framework drafting).

        Cycles are legal, so this loop is guarded three ways (column 16):
        the wave cap, the empty-ready :class:`Stalled` (unfinished nodes
        named), and the no-progress detector (waves executing without any
        shared-state change are a loop with no terminating write)."""
        max_waves = self._max_waves
        no_progress = 0
        last_shared_fingerprint: str | None = None

        for _ in range(max_waves):
            if plan.is_complete(run.node_states):
                return
            self._check_budget(run)
            # Route's roads not taken: a node whose every incoming edge is
            # waived will never run — skip it so the graph converges.
            for skipped in plan.skipped(run.node_states, run.shared):
                run.node_state(skipped.node_id).mark_skipped()
                logger.info("[Plan] node=%s skipped (conditional edge waived)", skipped.node_id)
            ready = plan.ready(run.node_states, shared=run.shared)
            if not ready:
                # Ready-empty with unfinished nodes is the stall signal
                # (column 16): a cycle whose conditions can never be
                # satisfied, or a deadlock — name them, never hang.
                unfinished = [
                    n.node_id
                    for n in plan.nodes.values()
                    if run.node_state(n.node_id).status
                    in (NodeStatus.PENDING, NodeStatus.RUNNING, NodeStatus.FAILED)
                ]
                raise Stalled(unfinished)

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
            # Wave barrier: declared-channel writes buffered during dispatch
            # fold here, through the same Update gate (event log + reducer
            # merge), after the same-wave conflict check fails closed.
            commands.extend(self._flush_wave_writes(plan))
            self._check_channel_conflicts(commands, plan)
            if commands:
                for applied_event in await self._apply_commands(commands, plan, run):
                    yield applied_event

            # The cycle engine's implicit decision, evented: a completed node
            # an active back edge points at goes PENDING again (goto commands
            # requeue through the same door). Runs before the park returns so
            # a suspending wave persists its requeues.
            await self._apply_requeues(batch, commands, plan, run)
            # Dynamic fan-out (column 17): Sends grow the plan by instances
            # that join the next wave root-ready.
            await self._apply_sends(commands, plan, run)

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
                await self._handle_failures(batch.failures, plan, run)
                return

            # No-progress detector: a wave executed but nothing in shared
            # state changed — a cycle whose body never writes its exit
            # condition's inputs can only be waiting for a miracle.
            fingerprint = repr(sorted(run.shared.items(), key=lambda kv: kv[0]))
            if outcomes and fingerprint == last_shared_fingerprint:
                no_progress += 1
                if no_progress >= _MAX_NO_PROGRESS_WAVES:
                    raise InfiniteLoopDetected(
                        f"no shared-state change across {no_progress} waves — "
                        "a cycle body must write what its exit condition reads"
                    )
            else:
                no_progress = 0
            last_shared_fingerprint = fingerprint
        else:
            # Wave cap exhausted mid-flight is a stall, not success.
            unfinished = [
                n.node_id
                for n in plan.nodes.values()
                if run.node_state(n.node_id).status
                not in (NodeStatus.COMPLETED, NodeStatus.SKIPPED)
            ]
            if unfinished:
                raise Stalled(unfinished, reason=f"wave budget of {max_waves} exhausted")

    async def _apply_requeues(
        self,
        batch: _BatchResult,
        commands: list[tuple[Node, Command]],
        plan: Plan,
        run: Run,
    ) -> None:
        """Requeue completed nodes the cycle engine points back at.

        Two doors, one mechanism (column 5/6): when a node succeeds, every
        *active* outgoing edge whose target is already COMPLETED requeues
        that target — a back edge restarting its loop, and a forward edge
        cascading a redo (the re-run source's old output is stale for its
        dependents). A *goto command* requeues its named target through the
        same door. PENDING targets are already queued; OBSOLETE is terminal
        and a goto at one is a blueprint bug, loudly reported."""
        requeues: list[tuple[str, str, str]] = []  # (target, source, via)

        for succ in batch.successes:
            for e in plan.outgoing(succ.node.node_id):
                if e.is_active(run.shared) and (
                    run.node_state(e.target).status is NodeStatus.COMPLETED
                ):
                    via = "back-edge" if e in plan.back_edges() else "redo"
                    requeues.append((e.target, succ.node.node_id, via))

        for node, command in commands:
            if isinstance(command, Goto):
                if command.target == WAIT:
                    # The join idiom (column 17): not all of the batch is in
                    # yet — sleep one wave and let me look again.
                    if run.node_state(node.node_id).status is NodeStatus.COMPLETED:
                        requeues.append((node.node_id, node.node_id, "wait"))
                    continue
                if plan.get_node(command.target) is None:
                    raise ValueError(
                        f"goto from {node.node_id!r}: target {command.target!r} "
                        "is not in the plan (declare it, or check the spelling)"
                    )
                status = run.node_state(command.target).status
                if status is NodeStatus.SKIPPED:
                    raise ValueError(
                        f"goto from {node.node_id!r}: target {command.target!r} "
                        "was scrapped (skipped or replaced) and cannot requeue"
                    )
                if status is NodeStatus.COMPLETED:
                    requeues.append((command.target, node.node_id, "goto"))

        for target, source, via in requeues:
            run.node_state(target).reset_to_pending()
            if self._log is not None:
                await self._log.record_node_requeued(plan, run, target, source=source, via=via)
            logger.info("[Plan] node=%s requeued via %s from %s", target, via, source)

    async def _apply_sends(
        self,
        commands: list[tuple[Node, Command]],
        plan: Plan,
        run: Run,
    ) -> None:
        """Materialize Send commands as template instances (column 17).

        The count is runtime data — a node returns one Send per item and the
        scheduler grows the plan by exactly that many instances, each
        root-ready for the next wave. Instances carry the template's body
        with the payload as params; their writes fold through the same
        channels (a merge channel keyed by item is how N results land
        without overwriting), and the instantiation is evented so the fold
        and a resume rebuild the exact instance set."""
        from prodagent.kernel.graph import Node as GraphNode
        from prodagent.kernel.graph import Origin, node_wire_dict
        from prodagent.kernel.node_state import NodeRuntimeState

        for node, command in commands:
            if not isinstance(command, Send):
                continue
            template = plan.get_node(command.template)
            if template is None:
                raise ValueError(
                    f"send from {node.node_id!r}: template {command.template!r} is not in the plan"
                )
            self._instance_seq += 1
            instance_id = f"{command.template}#{self._instance_seq}"
            instance = GraphNode(
                node_id=instance_id,
                body=template.body,
                params=dict(command.payload),
                origin=Origin.DYNAMIC,
            )
            plan.add_nodes([instance])
            run.node_states.setdefault(instance_id, NodeRuntimeState(instance_id))
            if self._log is not None:
                await self._log.record_node_instantiated(
                    plan, run, node_wire_dict(instance, run.node_states[instance_id])
                )
            logger.info(
                "[Plan] instantiated %s from template %s (sent by %s)",
                instance_id,
                command.template,
                node.node_id,
            )

    async def _dispatch_wave(
        self,
        ready: list[Node],
        plan: Plan,
        run: Run,
        outcomes_box: list[list[NodeOutcome]],
    ) -> AsyncGenerator[AgentEvent, None]:
        """Dispatch ready nodes with the ordering discipline of column 4's
        waves: readonly bodies run concurrently (bounded), write bodies one
        at a time. A write node streams its live events (a run-driving
        body's rounds) straight into the run's stream as it executes."""
        by_node: dict[str, NodeOutcome] = {}

        readonly = [n for n in ready if self._node_is_readonly(n)]
        writes = [n for n in ready if n.node_id not in {r.node_id for r in readonly}]

        if readonly:
            gate = self._readonly_gate

            async def _run_captured(node: Node) -> NodeOutcome | BaseException:
                """One readonly node as a structured task: failures come home
                as *data* (a sibling's crash never cancels its brothers —
                the wave classifies, it doesn't panic); only cancellation
                escapes the task, so an outer cancel reaps every sibling at
                their next await point. The semaphore rides an async-with —
                every exit path (return, raise, cancel) gives the slot back."""
                try:
                    if gate is None:
                        return await self._node_runner.run_one(node, plan, run)
                    async with gate:
                        return await self._node_runner.run_one(node, plan, run)
                except asyncio.CancelledError:
                    raise
                except BaseException as exc:  # noqa: BLE001 — node failure is data
                    return exc

            timeout_cm = (
                asyncio.timeout(self._wave_timeout)
                if self._wave_timeout is not None
                else contextlib.nullcontext()
            )
            async with timeout_cm, asyncio.TaskGroup() as tg:
                tasked = [tg.create_task(_run_captured(n)) for n in readonly]
            raw: list[NodeOutcome | BaseException] = [t.result() for t in tasked]
            for node, r in zip(readonly, raw, strict=True):
                if isinstance(r, asyncio.CancelledError):
                    raise r
                if isinstance(r, (BudgetExceeded, InfiniteLoopDetected, Stalled)):
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
                if getattr(node.body, "drives_run", False):
                    # A run-driving body's crash already floated through the
                    # node runner as a raise — it is the run's crash, not data.
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
        run: Run,
    ) -> list[AgentEvent]:
        """Apply the wave's state writes: every Update passes its runtime
        gate (a contested key demands a declared reducer), lands in the
        event log, and merges into the run's shared state — the data Route
        selectors and Loop predicates read."""
        events: list[AgentEvent] = []
        for node, command in commands:
            if isinstance(command, Update):
                # A declared channel owns its rule: a writer on one states
                # the *what*, the blueprint already stated the *how*.
                if command.reducer is None and command.key in plan.channels:
                    command = Update(command.key, command.value, plan.channels[command.key].reducer)
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

    def _flush_wave_writes(self, plan: Plan) -> list[tuple[Node, Command]]:
        """Drain the wave's buffered channel writes as Update commands.

        Folding at the barrier (not on write) is what makes same-wave reads
        see the wave-start snapshot and the merge order-free; routing the
        drained rows through the Update gate is what keeps the event log
        the replayable truth for channel state."""
        if not self._wave_writes:
            return []
        rows = self._wave_writes.drain()
        return [
            (
                plan.nodes[writer],
                Update(key=key, value=value, reducer=plan.channels[key].reducer),
            )
            for key, value, writer in rows
        ]

    def _check_channel_conflicts(self, commands: list[tuple[Node, Command]], plan: Plan) -> None:
        """Fail closed on same-wave multi-writers to an order-dependent rule.

        The buffered writes and any explicit Update commands on a declared
        channel both count — the conflict is about the wave, not the door
        the write came through."""
        if not plan.channels:
            return
        writers: dict[str, list[str]] = {}
        for node, command in commands:
            if isinstance(command, Update) and command.key in plan.channels:
                writers.setdefault(command.key, []).append(node.node_id)
        for key, ws in writers.items():
            if len(ws) > 1 and not plan.channels[key].is_order_independent:
                raise AmbiguousWrite(key, ws)

    def _node_is_readonly(self, node: Node) -> bool:
        # Bodies know their own side-effect class; only ToolBody defers to the
        # registry's metadata. No metadata → treat as a write (serial, safe).
        readonly = getattr(node.body, "readonly", None)
        if readonly is not None:
            return bool(readonly)
        if self._dispatcher is None:
            return False
        meta = self._dispatcher.meta_for(str(getattr(node.body, "target", "")))
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
    def _emit_node_events(batch: _BatchResult, run: Run) -> Iterator[AgentEvent]:
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
        run: Run,
    ) -> None:
        """A failed node ends the run (column 24's stance, reversed from the
        old replanner): models produce task lists, not graphs — there is no
        in-framework drafting to ask for a replacement. The doomed downstream
        is quarantined (SKIPPED, never deleted), every failure recorded, and
        the run fails with the primary's crash scene. Recovery is the
        application's composition: a back edge, a retry policy, or a new run."""
        primary, *secondary = failures
        for fail in secondary:
            await self._record_failure(fail, plan, run)
        await self._record_failure(primary, plan, run)
        run.fail(f"node {primary.node.node_id!r} failed: {primary.error}")

    async def _record_failure(
        self,
        failure: NodeFailed,
        plan: Plan,
        run: Run,
    ) -> None:
        node = failure.node
        exc = failure.error
        run.node_state(node.node_id).mark_failed(str(exc))
        run.tool_failures += 1
        quarantined = plan.mark_downstream_skipped(node.node_id, run.node_states)
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
        logger.error("[Plan] node=%s FAILED: %s  quarantined=%s", node.node_id, exc, quarantined)
