"""scheduler — the kernel's single engine: repeatedly compute "who is ready
now" and advance wave by wave.

The main loop is deliberately short, because the earlier parts absorb the
complexity:

    while running:
        ready   = plan.ready(run)           # 1) along edges, who is ready this wave
        results = run ready concurrently    # 2) wave concurrency (bounded), no direct shared-state mutation
        barrier: fold deltas / apply commands  # 3) commit together at wave end (facts already durable)

Three key properties:
- the wave is a consistency boundary: while running, nodes only produce
  Outcomes and never write shared state directly; everything is folded by
  reducers at the barrier, so concurrent results are deterministic and the
  barrier is naturally a commit point;
- suspension is "letting go": when a node requests an Interrupt, the wave lets
  the other nodes finish, then parks as a whole (its facts are already durable
  in the log); on resume the parked nodes re-run with the external values fed
  back; failure keeps the same discipline — the wave settles, then the Run
  stops;
- multi-agent adds no new engine: SubPlanBody recursively runs a child Run via
  the activation port — still right here.
"""

from __future__ import annotations

import asyncio
import dataclasses
from typing import Any

from src.kernel.blob import InMemoryBlobStore
from src.kernel.body import NodeContext, Outcome
from src.kernel.bus import Bus
from src.kernel.channels import WaveWrites
from src.kernel.command import Goto, Send
from src.kernel.eventlog import (
    CONTROL,
    DELEGATED,
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
)
from src.kernel.graph import Plan
from src.kernel.replay import replay
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

    async def activate(
        self,
        spec: Plan,
        task: str,
        parent_run: Run,
        node_id: str = "",
        *,
        input: dict | None = None,
        llm: Any = None,
        tools: Any = None,
    ) -> dict:
        # Born through the parent: Run.child_of computes the depth ledger. The
        # caller supplies the opening state update (``input``) and any explicit
        # identity (llm/tools); the kernel neither builds the seed nor knows a
        # conversation channel. drive resolves any unbound ports at its door.
        child = Run.child_of(parent_run, spec, task=task, input=input, llm=llm, tools=tools)
        # the delegation fact, on the parent's log: after a crash the child's
        # run_id lives here, so the child can be re-attached instead of orphaned
        await self.scheduler._commit(
            parent_run, DELEGATED, {"node": node_id, "child_run_id": child.run_id}
        )
        await self.scheduler.drive(spec, child)
        # Report the child's terminal state honestly; the caller decides what it
        # means — SubPlanBody fails its node, an agent-as-tool raises HardToolError.
        # (The depth-guard RecursionError is a structural error raised above.)
        result = {
            "run_id": child.run_id,
            "state": str(child.state),
            "output": child.final_output,
            "shared": child.shared,
        }
        if child.state == RunState.FAILED:
            result["error"] = str(child.final_output)
        if child.state == RunState.SUSPENDED:
            # the caller parks too (SubPlanBody translates): surface the child's
            # question so the parent's park can ask the same thing
            it = next(iter(child.interrupts.values()), None)
            result["question"] = it.question if it else ""
        return result


class Scheduler:
    def __init__(
        self,
        *,
        llm: Any = None,
        tools: Any = None,
        bus: Bus | None = None,
        eventlog: Any = None,
        blobs: Any = None,
        max_waves: int = 64,
        concurrency: int = 8,
    ):
        self.llm = llm
        self.tools = tools
        self.bus = bus or Bus()
        self.eventlog = eventlog or InMemoryEventLog()
        # Artifact bytes; in-memory by default (zero side effect), swap in a
        # local-directory store from backends for durable files.
        self.blobs = blobs if blobs is not None else InMemoryBlobStore()
        self.max_waves = max_waves
        self.concurrency = concurrency  # per-Run cap on nodes running at once
        self.subagent = InProcessActivator(self)

    # — main public entry —
    async def run(self, plan: Plan, *, task: str = "", input: dict | None = None) -> Run:
        # The caller supplies the opening state update (``input``); Scheduler.drive
        # resolves identity ports once at the engine door, however the Run was born.
        run = Run.start(plan, task=task, input=input)
        await self.drive(plan, run)
        return run

    async def resume(
        self, plan: Plan, run_id: str, value: Any = None, *, llm: Any = None, tools: Any = None
    ) -> Run:
        """Resume an unfinished run from its recorded facts: rebuild the Run by
        replaying its event stream, feed back one value per parked node, then
        re-drive. The log is the only durable thing — there is no snapshot to
        load; resume is a pure function of (events, value).

        ``value`` is normally the bare payload for the one parked node. If
        several nodes parked in the same wave, pass a ``{node_id: value}``
        dict whose keys exactly match the parked set. ``llm``/``tools`` rebind
        the restored Run to its Agent identity (ports are wiring, never logged).

        A run that crashed mid-wave also lands here: its replayed state is
        RUNNING with in-flight nodes, which are re-armed and re-driven —
        at-least-once, so a side effect may re-execute (the ToolCall contract
        says the same). Finished runs refuse: their facts are history.
        """
        run = replay(plan, await self.eventlog.events(run_id))
        # Re-attach explicit identity (the Agent's own model/registry); an
        # unbound Run inherits the host ports when drive begins — one rule.
        run.llm = llm
        run.tools = tools
        if run.state not in (RunState.SUSPENDED, RunState.RUNNING):
            # fail-wins waves leave parked facts on a finished Run — history, not to resume
            raise RuntimeError(
                f"run {run_id} is {run.state}; only a suspended or crashed (running) run can resume"
            )
        if run.state == RunState.RUNNING:
            # crash continue: re-arm what was in flight — a derived recovery
            # adaptation (their node_started facts already say "was running"),
            # not a new fact, so it stays scheduler-side.
            for key, st in run.node_states.items():
                if st.status == NodeStatus.RUNNING:
                    run.rearm(key, immediate=True)  # faithful: it was in flight
            await self.drive(plan, run)
            return run
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
        # the answer is a fact like the question: apply(RESUMED) resumes with
        # the values and re-arms every parked node — one fact, whole transition
        await self._commit(run, RESUMED, {"nodes": parked, "values": values})
        await self.drive(plan, run)
        return run

    # — engine main loop —
    async def drive(self, plan: Plan, run: Run) -> None:
        # The engine door is the one place identity ports are resolved: keep an
        # explicit binding (an Agent's own model/registry on a child or a
        # resumed Run); an unbound Run (a bare graph, or one built by hand then
        # driven) inherits the Scheduler's defaults. Bodies then just read
        # run.llm / run.tools — no body ever re-checks a fallback again.
        if run.llm is None:
            run.llm = self.llm
        if run.tools is None:
            run.tools = self.tools
        # one pool per Run: a delegation chain never waits on its own slots
        sem = asyncio.Semaphore(self.concurrency)
        if run.event_seq == 0 and run.state == RunState.RUNNING:
            # A ledger fact decides ledger facts: event_seq == 0 means this
            # stream has not opened yet (a replayed run never re-opens — and
            # apply would refuse a second opening besides). depth rides the
            # fact so a cross-process resume keeps the tree guard.
            await self._commit(
                run, RUN_STARTED, {"task": run.task, "name": run.name, "depth": run.depth}
            )
            if run.seed:  # the opening state update becomes one fact like any other
                seed_writes = WaveWrites(plan.channels)
                for key, value in run.seed.items():
                    seed_writes.buffer(key, value, "<seed>")
                delta = run.wave_delta(seed_writes.drain(), plan.channels)
                run.seed = {}
                if delta:
                    await self._commit(run, STATE_DELTA, {"delta": delta})

        while run.running:
            ready, swept = self._next_ready(plan, run)
            for key in swept:  # each structurally-skipped branch is a fact
                await self._commit(run, NODE_SKIPPED, {"node": key})
            if not ready:
                if swept:
                    continue  # a fresh skip can free a join — recompute before settling
                await self._settle(plan, run)
                break

            run.metrics["waves"] += 1
            if run.metrics["waves"] > self.max_waves:
                await self._commit(
                    run,
                    RUN_FAILED,
                    {
                        "reason": f"exceeded max waves {self.max_waves}; suspected spin "
                        "(check that a back-edge makes progress)"
                    },
                )
                break

            # 2) Wave concurrency: nodes share no mutable state, each yields an Outcome.
            results = await asyncio.gather(*[self._run_node(plan, run, key, sem) for key in ready])

            # 3) Barrier: handle results together. Failure is fail-fast: it stops
            # the Run, not the settlement — every started node still settles.
            # Each settled node becomes exactly one fact; apply is what marks
            # it (and consumes its Goto input at terminal states).
            parked: dict[str, Any] = {}
            controls: list[tuple[str, Any]] = []
            writes = WaveWrites(plan.channels)
            failed: tuple[str, str] | None = None

            for key, outcome, error in results:
                if error is not None:
                    # str form, not repr: a delegation cascade embeds this text
                    # again at each level, and repr would re-escape the quotes
                    err = f"{type(error).__name__}: {error}"
                    await self._commit(run, NODE_FAILED, {"node": key, "error": err})
                    failed = failed or (key, err)
                elif outcome.suspend is not None:
                    parked[key] = dataclasses.replace(outcome.suspend, node_id=key)
                    continue  # a park keeps its Goto input for the re-run
                else:
                    for k, v in outcome.state_delta.items():
                        writes.buffer(k, v, key)
                    if outcome.control is not None:
                        controls.append((key, outcome.control))
                    await self._commit(run, NODE_COMPLETED, {"node": key, "output": outcome.value})

            if failed is None:
                writes.check_ambiguous()  # an already-failing wave still folds what succeeded
            delta = run.wave_delta(writes.drain(), plan.channels)
            if delta:
                await self._commit(run, STATE_DELTA, {"delta": delta})

            if parked:  # the ask is a fact whether the run then parks or fails
                await self._commit(run, INTERRUPTED, _parked_facts(parked))

            if failed is not None:
                await self._commit(run, RUN_FAILED, {"node": failed[0], "reason": failed[1]})
                break

            invalid = _invalid_control(plan, controls)
            if invalid is not None:
                writer, reason = invalid
                # A run-level failure: the writer node itself completed, it only
                # emitted an illegal command, so the event carries no failed node
                # (unlike a node that raised, which is marked failed).
                await self._commit(run, RUN_FAILED, {"reason": f"{reason} (from node {writer!r})"})
                break

            await self._apply_controls(run, controls)

            if parked:
                break  # the suspension facts are already durable in the log

    def _next_ready(self, plan: Plan, run: Run) -> tuple[list[str], list[str]]:
        """Who can run now, plus any nodes newly swept as dead branches. The
        sweep is pure: its marks land only when the caller commits the
        node_skipped facts, so a skip that frees a join shows up on the next
        loop turn, not this one. An empty fan-out converges last."""
        # Deliberately simple: recompute readiness from scratch each wave —
        # O(nodes x preds), up to 3x on the stall path — clarity wins at scale 0.
        ready = plan.ready(run)
        if ready:
            return ready, []
        swept = plan.sweep_skipped(run)
        return plan.ready(run, empty_fanout=True), swept

    # — executing a single node —
    async def _run_node(
        self, plan: Plan, run: Run, key: str, sem: asyncio.Semaphore
    ) -> tuple[str, Outcome | None, BaseException | None]:
        resume_value = run.resume_values.get(key)  # peek before the fact consumes it
        await self._commit(run, NODE_STARTED, {"node": key})  # apply marks + consumes

        async def record(kind: str, data: dict | None = None) -> None:
            # The ledger's write side: append one durable fact to this run's log.
            await self._commit(run, kind, data)

        async def facts() -> list[Event]:
            # The ledger's read side: this run's facts so far, oldest first.
            # The log belongs to the Scheduler; a body reaches it only through
            # this pair — it never holds the ledger itself.
            return await self.eventlog.events(run.run_id)

        ctx = NodeContext(
            run,
            key,
            llm=run.llm,  # identity ports were resolved once at birth/resume
            tools=run.tools,
            subagent=self.subagent,
            bus=self.bus,
            blobs=self.blobs,
            record=record,
            facts=facts,
            resume_value=resume_value,
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
                await self._commit(
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
    async def _apply_controls(self, run: Run, controls: list[tuple[str, Any]]) -> None:
        for _writer, control in controls:
            commands = control if isinstance(control, list) else [control]
            for cmd in commands:
                if isinstance(cmd, Goto):
                    # immediate: re-arm + release now (back-edge/jump/handover);
                    # otherwise only re-arm — readiness is still decided by
                    # incoming edges and join. The fact says it; apply does it.
                    await self._commit(
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
                    await self._commit(
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
            await self._commit(run, RUN_COMPLETED, {"output": self._final_output(plan, run)})
        else:
            pending = [
                k
                for k, s in run.node_states.items()
                if s.status == NodeStatus.PENDING
                and not (k in plan.nodes and plan.nodes[k].template)
            ]
            await self._commit(
                run,
                RUN_FAILED,
                {
                    "reason": f"graph stalled: no ready node but unfinished nodes remain "
                    f"{pending} (an edge is likely mis-wired)"
                },
            )

    def _final_output(self, plan: Plan, run: Run) -> Any:
        # Take only convergence nodes that actually completed; branches
        # structurally skipped by conditional edges don't enter the final result.
        terms = [t for t in plan.terminal_ids() if run.is_completed(t)]
        values = {t: run.state_of(t).output for t in terms}
        if len(values) == 1:
            return next(iter(values.values()))
        return values

    # — commit: the only way facts are born —
    async def _commit(self, run: Run, kind: str, data: dict | None = None) -> None:
        """Append one fact durably, then apply it, then announce it.

        The log precedes its projection, and Run.apply is the single mutation
        path — the replay side runs the very same apply, so a live Run and its
        replay cannot diverge by discipline. A rejected apply (e.g. a second
        run_started) fails loudly right where the fact is made.
        """
        ev = Event(run.event_seq + 1, run.run_id, kind, data or {}, parent_id=run.parent_id)
        await self.eventlog.append(ev)
        run.apply(ev)
        await self.bus.fire(kind, evt=ev)
