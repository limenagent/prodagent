"""scheduler — the kernel's single engine: repeatedly compute "who is ready
now" and advance wave by wave.

The main loop is deliberately short, because the earlier parts absorb the
complexity:

    while running:
        ready   = plan.ready(run)           # 1) along edges, who is ready this wave
        results = run ready concurrently    # 2) wave concurrency (bounded), no direct shared-state mutation
        barrier: fold deltas / apply commands / checkpoint  # 3) commit together at wave end

Three key properties:
- the wave is a consistency boundary: while running, nodes only produce
  Outcomes and never write shared state directly; everything is folded by
  reducers at the barrier, so concurrent results are deterministic and the
  barrier is naturally a commit point;
- suspension is "letting go": when a node requests an Interrupt, the wave lets
  the other nodes finish, then persists and pauses as a whole; on resume only
  that one node re-runs with the external input fed back; failure keeps the
  same discipline — the wave settles, then the Run stops;
- multi-agent adds no new engine: SubPlanBody recursively runs a child Run via
  the activation port — still right here.
"""

from __future__ import annotations

import asyncio
import dataclasses
from typing import Any

from src.kernel.body import NodeContext, Outcome
from src.kernel.bus import Bus
from src.kernel.channels import WaveWrites
from src.kernel.command import Goto, Send
from src.kernel.eventlog import (
    CONTROL,
    INTERRUPTED,
    NODE_COMPLETED,
    NODE_FAILED,
    NODE_RETRY,
    NODE_SKIPPED,
    NODE_STARTED,
    RESUMED,
    RUN_COMPLETED,
    RUN_FAILED,
    RUN_STARTED,
    STATE_DELTA,
    Event,
    InMemoryEventLog,
    InMemoryStore,
)
from src.kernel.graph import Plan
from src.kernel.run import Run
from src.kernel.types import NodeStatus, RunState


def _invalid_control(plan: Plan, controls: list[tuple[str, Any]]) -> tuple[str, str] | None:
    for writer, control in controls:
        commands = control if isinstance(control, list) else [control]
        for cmd in commands:
            if isinstance(cmd, Goto):
                target = plan.nodes.get(cmd.target)
                if target is None:
                    return writer, f"Goto target {cmd.target!r} does not exist"
                if target.template:
                    return writer, f"Goto target {cmd.target!r} is a template node; use Send"
            elif isinstance(cmd, Send):
                target = plan.nodes.get(cmd.template)
                if target is None:
                    return writer, f"Send template {cmd.template!r} does not exist"
                if not target.template:
                    return writer, f"Send target {cmd.template!r} is not a template node"
    return None


def _parked_facts(parked: dict[str, Any]) -> dict[str, Any]:
    # Full parked facts (question/payload): the stream alone rebuilds the suspension.
    return {
        "parked": {
            key: {"kind": it.kind, "question": it.question, "payload": it.payload}
            for key, it in parked.items()
        },
    }


class InProcessActivator:
    """Default sub-agent activator: recursively run a child Plan in-process
    with the same scheduler (call semantics).

    A remote implementation (A2A, RPC) only has to satisfy the SubagentPort
    protocol, without changing a line of the kernel — "where it runs" lives
    behind the port (location transparency).
    """

    def __init__(self, scheduler: Scheduler):
        self.scheduler = scheduler

    async def activate(self, spec: Plan, task: str, parent_run: Run, payload: Any = None) -> dict:
        # the depth limit itself lives at Run birth (run.py), not here
        child = Run.start(spec, parent_id=parent_run.run_id, depth=parent_run.depth + 1, task=task)
        await self.scheduler.drive(spec, child)
        if child.state == RunState.FAILED:
            # Call semantics: a delegated child Run that failed cannot be swallowed
            # as a "normal output" by the parent; failure propagates up the Run tree
            # (the depth-guard RecursionError reaches the root this way too).
            raise RuntimeError(f"child Run {child.run_id} failed: {child.final_output}")
        return {
            "run_id": child.run_id,
            "state": str(child.state),
            "output": child.final_output,
            "shared": child.shared,
        }


class Scheduler:
    def __init__(
        self,
        *,
        llm: Any = None,
        tools: Any = None,
        bus: Bus | None = None,
        eventlog: Any = None,
        store: Any = None,
        max_waves: int = 64,
        concurrency: int = 8,
        durability: str = "sync",
    ):
        if durability not in ("sync", "exit"):
            raise ValueError("durability must be 'sync' or 'exit'")
        self.llm = llm
        self.tools = tools
        self.bus = bus or Bus()
        self.eventlog = eventlog or InMemoryEventLog()
        self.store = store or InMemoryStore()
        self.max_waves = max_waves
        self.concurrency = concurrency  # per-Run cap on nodes running at once
        # sync: checkpoint after every wave; exit: only when suspended or finished.
        self.durability = durability
        self.subagent = InProcessActivator(self)

    # — main public entry —
    async def run(self, plan: Plan, *, task: str = "") -> Run:
        run = Run.start(plan, task=task)
        await self.drive(plan, run)
        return run

    async def resume(self, plan: Plan, run_id: str, value: Any = None) -> Run:
        """Resume from a checkpoint: feed back one value per parked node, then
        re-run just those nodes and continue.

        ``value`` is normally the bare payload for the one parked node. If
        several nodes parked in the same wave, pass a ``{node_id: value}``
        dict whose keys exactly match the parked set.
        """
        snap = await self.store.load(run_id)
        if snap is None:
            raise KeyError(f"no checkpoint for {run_id}; cannot resume")
        run = Run.restore(plan, snap)
        if run.state != RunState.SUSPENDED:
            # fail-wins waves leave parked facts on a finished Run — history, not to resume
            raise RuntimeError(f"run {run_id} is {run.state}; only a suspended run can resume")
        parked = list(run.interrupts)
        # A dict is only a {node_id: value} mapping if its keys are exactly the
        # parked set; otherwise (including a single parked node whose own
        # payload happens to be a dict) it is the bare value for that one node.
        if isinstance(value, dict) and set(value) == set(parked):
            values = value
        elif len(parked) == 1:
            values = {parked[0]: value}
        else:
            raise KeyError(f"resume value(s) must cover every parked node: {parked}")
        run.resume(values)
        for node_id in parked:
            run.rearm(node_id, immediate=True)
        await self._emit(run, RESUMED, {"nodes": parked})
        await self.drive(plan, run)
        return run

    # — engine main loop —
    async def drive(self, plan: Plan, run: Run) -> None:
        # one pool per Run: a delegation chain never waits on its own slots
        sem = asyncio.Semaphore(self.concurrency)
        if run.metrics["waves"] == 0 and run.state == RunState.RUNNING:
            await self._emit(run, RUN_STARTED, {"task": run.task, "name": run.name})
            if run.seed:  # fold initial input through the same reducers and log it as a fact
                seed_writes = WaveWrites(plan.channels)
                for key, value in run.seed.items():
                    seed_writes.buffer(key, value, "<seed>")
                folded = run.fold_writes(seed_writes.drain(), plan.channels)
                run.seed = {}
                if folded:
                    await self._emit(run, STATE_DELTA, {"delta": folded})

        while run.running:
            ready, swept = self._next_ready(plan, run)
            for key in swept:  # record each structurally-skipped branch as a fact
                await self._emit(run, NODE_SKIPPED, {"node": key})
            if not ready:
                await self._settle(plan, run)
                break

            run.metrics["waves"] += 1
            if run.metrics["waves"] > self.max_waves:
                run.fail(
                    f"exceeded max waves {self.max_waves}; suspected spin (check that a back-edge makes progress)"
                )
                await self._emit(run, RUN_FAILED, {"reason": run.final_output})
                break

            # 2) Wave concurrency: nodes share no mutable state, each yields an Outcome.
            results = await asyncio.gather(*[self._run_node(plan, run, key, sem) for key in ready])

            # 3) Barrier: handle results together. Failure is fail-fast: it stops
            # the Run, not the settlement — every started node still settles.
            parked: dict[str, Any] = {}
            controls: list[tuple[str, Any]] = []
            writes = WaveWrites(plan.channels)
            failed: tuple[str, str] | None = None

            for key, outcome, error in results:
                if error is not None:
                    # str form, not repr: a delegation cascade embeds this text
                    # again at each level, and repr would re-escape the quotes
                    err = f"{type(error).__name__}: {error}"
                    run.mark_failed(key, err)
                    await self._emit(run, NODE_FAILED, {"node": key, "error": err})
                    failed = failed or (key, err)
                elif outcome.suspend is not None:
                    parked[key] = dataclasses.replace(outcome.suspend, node_id=key)
                    continue  # a park keeps its Goto input for the re-run
                else:
                    run.mark_completed(key, outcome.value)
                    for k, v in outcome.state_delta.items():
                        writes.buffer(k, v, key)
                    if outcome.control is not None:
                        controls.append((key, outcome.control))
                    await self._emit(run, NODE_COMPLETED, {"node": key, "output": outcome.value})
                run.deliveries.pop(key, None)  # a terminal state consumes its Goto input

            if failed is None:
                writes.check_ambiguous()  # an already-failing wave still folds what succeeded
            folded = run.fold_writes(writes.drain(), plan.channels)
            if folded:
                await self._emit(run, STATE_DELTA, {"delta": folded})

            if parked:  # the ask is a fact whether the run then parks or fails
                run.suspend(parked)
                await self._emit(run, INTERRUPTED, _parked_facts(parked))

            if failed is not None:
                run.fail(failed[1])
                await self._emit(run, RUN_FAILED, {"node": failed[0], "reason": failed[1]})
                break

            invalid = _invalid_control(plan, controls)
            if invalid is not None:
                writer, reason = invalid
                # A run-level failure: the writer node itself completed, it only
                # emitted an illegal command, so the event carries no failed node
                # (unlike a node that raised, which is marked failed).
                run.fail(f"{reason} (from node {writer!r})")
                await self._emit(run, RUN_FAILED, {"reason": run.final_output})
                break

            await self._apply_controls(plan, run, controls)

            if parked:
                await self._checkpoint(run)  # a suspended run must always be durable
                break

            if self.durability == "sync":
                await self._checkpoint(run)

        if run.state in (RunState.COMPLETED, RunState.FAILED):
            await self._checkpoint(run)  # the terminal fact is always durable

    def _next_ready(self, plan: Plan, run: Run) -> tuple[list[str], list[str]]:
        """Who can run now, plus any nodes newly swept as dead branches. If
        nobody is ready, first sweep untaken conditional edges (the sweep
        cascades), recompute, then finally let an empty fan-out converge."""
        # Deliberately simple: recompute readiness from scratch each wave —
        # O(nodes x preds), up to 3x on the stall path — clarity wins at scale 0.
        ready = plan.ready(run)
        if ready:
            return ready, []
        swept = plan.sweep_skipped(run)
        ready = plan.ready(run)
        return (ready or plan.ready(run, empty_fanout=True)), swept

    # — executing a single node —
    async def _run_node(
        self, plan: Plan, run: Run, key: str, sem: asyncio.Semaphore
    ) -> tuple[str, Outcome | None, BaseException | None]:
        run.mark_running(key)
        await self._emit(run, NODE_STARTED, {"node": key})
        ctx = NodeContext(
            run,
            key,
            llm=self.llm,
            tools=self.tools,
            subagent=self.subagent,
            bus=self.bus,
            resume_value=run.take_resume(key),
        )
        try:
            async with sem:  # this Run's wave concurrency cap
                node = plan.nodes[run.template_of(key)]
                outcome = await self._run_body(
                    node, self._node_input(plan, run, key), ctx, run, key
                )
            return key, outcome, None
        except asyncio.CancelledError:
            raise  # external cancellation is not a node failure; propagate unchanged
        except BaseException as exc:  # hand back to the barrier for uniform handling
            return key, None, exc

    async def _run_body(
        self, node: Any, value: Any, ctx: NodeContext, run: Run, key: str
    ) -> Outcome:
        """Run a node's body wrapped in the step-level resilience layer of
        "timeout + retry with backoff".

        The mechanism is fixed: a timeout counts as one failure, and after a
        failure the policy decides whether to try again. The policy itself (how
        many tries, how long to wait, which errors are worth retrying) hangs on
        the Node and is fully replaceable. External cancellation is not a
        "retryable failure" and must propagate unchanged.
        """
        policy = node.retry
        attempts = policy.max_attempts if policy else 1
        last_exc: BaseException | None = None
        for attempt in range(attempts):
            try:
                if node.timeout is not None:
                    return await asyncio.wait_for(node.body.run(value, ctx), node.timeout)
                return await node.body.run(value, ctx)
            except asyncio.CancelledError:
                raise  # cancelled externally: stop now, no retry
            except BaseException as exc:
                last_exc = exc
                can_retry = policy is not None and attempt + 1 < attempts and policy.matches(exc)
                if not can_retry:
                    raise
                backoff = policy.delay_for(attempt)
                await self._emit(
                    run,
                    NODE_RETRY,
                    {"node": key, "attempt": attempt + 1, "error": repr(exc), "backoff": backoff},
                )
                await asyncio.sleep(backoff)
        assert last_exc is not None
        raise last_exc

    def _node_input(self, plan: Plan, run: Run, key: str) -> Any:
        if run.is_instance(key):
            return run.input_of(key)
        if key in run.deliveries:
            # Input carried by a Goto transition: read here, consumed when the
            # node reaches a terminal state — a park keeps it for the re-run.
            return run.deliveries[key]
        preds = plan.incoming(key)
        if not preds:
            return run.task
        upstream: dict[str, Any] = {}
        for e in preds:
            src = e.source
            node = plan.nodes[src]
            if node.template:  # template predecessor: aggregate all its instances' outputs
                vals = [
                    run.state_of(k).output
                    for k in run.instances.get(src, ())
                    if run.is_completed(k)
                ]
                upstream[src] = vals
            elif run.is_completed(src) and plan.edge_live(e, run.shared):
                # Take a predecessor's output only along an edge that is "live now":
                # a branch not selected by a conditional edge feeds no input,
                # otherwise an untaken predecessor in an exclusive branch would pad
                # the input with a spurious dict.
                upstream[src] = run.state_of(src).output
        if len(upstream) == 1:
            return next(iter(upstream.values()))
        return upstream

    # — control commands: they change "the next wave's ready set" —
    async def _apply_controls(self, plan: Plan, run: Run, controls: list[tuple[str, Any]]) -> None:
        for _writer, control in controls:
            commands = control if isinstance(control, list) else [control]
            for cmd in commands:
                if isinstance(cmd, Goto):
                    # immediate: re-arm + release now (back-edge/jump/handover);
                    # otherwise only re-arm — readiness is still decided by
                    # incoming edges and join (an iterative convergence point
                    # waits for preds).
                    run.rearm(cmd.target, immediate=cmd.immediate)
                    if cmd.payload is not None:
                        run.deliveries[cmd.target] = (
                            cmd.payload
                        )  # transition input, symmetric with Send
                    await self._emit(
                        run,
                        CONTROL,
                        {
                            "op": "goto",
                            "target": cmd.target,
                            "immediate": cmd.immediate,
                            **({"payload": cmd.payload} if cmd.payload is not None else {}),
                        },
                    )
                elif isinstance(cmd, Send):
                    run.add_instance(cmd.template, cmd.payload, cmd.key)
                    await self._emit(
                        run,
                        CONTROL,
                        {
                            "op": "send",
                            "template": cmd.template,
                            **({"key": cmd.key} if cmd.key is not None else {}),
                            "payload": cmd.payload,
                        },
                    )
                else:
                    raise TypeError(f"unknown control command: {cmd!r}")

    # — settle —
    async def _settle(self, plan: Plan, run: Run) -> None:
        # The terminal fact goes into the event stream too: a replay must be
        # able to tell that — and how — the run ended, not just how it went.
        if plan.is_done(run):
            run.complete(self._final_output(plan, run))
            await self._emit(run, RUN_COMPLETED, {"output": run.final_output})
        else:
            pending = [
                k
                for k, s in run.node_states.items()
                if s.status == NodeStatus.PENDING
                and not (k in plan.nodes and plan.nodes[k].template)
            ]
            run.fail(
                f"graph stalled: no ready node but unfinished nodes remain {pending} (an edge is likely mis-wired)"
            )
            await self._emit(run, RUN_FAILED, {"reason": run.final_output})

    def _final_output(self, plan: Plan, run: Run) -> Any:
        # Take only convergence nodes that actually completed; branches
        # structurally skipped by conditional edges don't enter the final result.
        terms = [t for t in plan.terminal_ids() if run.is_completed(t)]
        values = {t: run.state_of(t).output for t in terms}
        if len(values) == 1:
            return next(iter(values.values()))
        return values

    # — events and checkpoints —
    async def _emit(self, run: Run, kind: str, data: dict | None = None) -> None:
        run.event_seq += 1
        event = Event(run.event_seq, run.run_id, kind, data or {}, parent_id=run.parent_id)
        await self.eventlog.append(event)
        await self.bus.fire(kind, evt=event)

    async def _checkpoint(self, run: Run) -> None:
        await self.store.save(run.run_id, run.snapshot())
