"""replay — project a Run back out of its recorded event stream.

It is a second *reader* of the same facts, never a second scheduler. The live
Scheduler drives Run's bookkeeping from the edges (it computes readiness, runs
bodies, calls the model and tools); replay drives the very same bookkeeping
methods from the log, in order, and never touches any of those:

    no ready(), no body, no LLM, no tool — only mark_*/add_instance/suspend/...

What the stream reproduces: shared state, every node's status/output/attempts,
dynamic instances and their inputs, goto deliveries, suspensions (with their
questions), and how the run ended. Two things are deliberately not fabricated:

- coarse counters (waves / llm / tool / tokens) live in checkpoints, not the
  stream, so a replayed run leaves them at their defaults;
- crash "continue from here" is snapshot restore's job (scheduler.resume).
  Replay is for audit, time travel, and understanding what happened.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from src.kernel.eventlog import (
    CONTROL,
    INTERRUPTED,
    NODE_COMPLETED,
    NODE_SKIPPED,
    NODE_STARTED,
    RESUMED,
    RUN_COMPLETED,
    RUN_FAILED,
    RUN_STARTED,
    STATE_DELTA,
    Event,
    apply_event,
)
from src.kernel.run import Interrupt, Run

# Events that only annotate (no settled bookkeeping): NODE_RETRY leaves the node
# running and is consumed by observers/tests, so projection ignores it.


def replay(plan: Any, events: Iterable[Event]) -> Run:
    """Rebuild a Run by folding its event stream. The plan is the same blueprint
    that produced the run (its static nodes/edges/channels are not in the log)."""
    run: Run | None = None

    for ev in events:
        if ev.kind == RUN_STARTED:
            run = Run.start(
                plan, run_id=ev.run_id, parent_id=ev.parent_id, task=ev.data.get("task", "")
            )
            run.event_seq = ev.seq
            continue
        if run is None:
            continue
        run.event_seq = ev.seq
        d = ev.data

        if ev.kind == NODE_STARTED:
            key = d["node"]
            # Mirror _node_input: a static node consumes the input a Goto handed
            # it exactly when it starts; a dynamic instance reads its own input.
            if key in run.deliveries and not run.is_instance(key):
                run.deliveries.pop(key)
            run.mark_running(key)
        elif ev.kind == NODE_COMPLETED:
            run.mark_completed(d["node"], d.get("output"))
        elif ev.kind == NODE_SKIPPED:
            run.mark_skipped(d["node"])
        elif ev.kind == STATE_DELTA:
            apply_event(run.shared, ev, plan.channels)
        elif ev.kind == CONTROL:
            if d["op"] == "goto":
                run.rearm(d["target"], immediate=d.get("immediate", True))
                if d.get("payload") is not None:
                    run.deliveries[d["target"]] = d["payload"]
            else:  # "send": instantiate a template copy (full key derives in order)
                run.add_instance(d["template"], d.get("payload"), d.get("key"))
        elif ev.kind == INTERRUPTED:
            run.suspend(
                {
                    node: Interrupt(
                        p.get("kind", "external"),
                        p.get("payload"),
                        p.get("question", ""),
                        node,
                    )
                    for node, p in d.get("parked", {}).items()
                }
            )
        elif ev.kind == RESUMED:
            # Flip the state back to running; the parked nodes' own node_started
            # events follow and re-mark them, so no rearm is needed here.
            run.resume(None)
        elif ev.kind == RUN_COMPLETED:
            run.complete(d.get("output"))
        elif ev.kind == RUN_FAILED:
            # "node" is present only when a node's body raised (that node is
            # marked failed); a run-level failure (stall / illegal control)
            # carries just a reason and leaves node states untouched.
            if d.get("node"):
                run.mark_failed(d["node"], d.get("reason", ""))
            run.fail(d.get("reason", ""))

    if run is None:
        raise ValueError("cannot replay: the event stream has no run_started event")
    return run
