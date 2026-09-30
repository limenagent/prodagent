"""replay — project a Run back out of its recorded event stream.

It is a second *reader* of the same facts, never a second scheduler: identity
is born from the opening fact, and every subsequent fact goes through
``Run.apply`` — the very same single mutation throat the live scheduler
commits through. There is nothing else here to drift:

    no ready(), no body, no LLM, no tool — construct + for ev: run.apply(ev)

What the stream reproduces: shared state, every node's status/output/attempts,
dynamic instances and their inputs, goto deliveries, suspensions (with their
questions), the answers fed back on resume, and how the run ended. Two things
are deliberately not fabricated:

- coarse counters (waves / llm / tool / tokens) are engine telemetry, not
  facts: a replayed run leaves them at their defaults;
- crash "continue from here" is this same function's job — scheduler.resume
  replays the stream and re-drives, so there is one recovery verb, not two.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from src.kernel.eventlog import RUN_STARTED, Event
from src.kernel.run import Run


def replay(plan: Any, events: Iterable[Event]) -> Run:
    """Rebuild a Run by folding its event stream. The plan is the same blueprint
    that produced the run (its static nodes/edges/channels are not in the log)."""
    plan.validate()
    events = list(events)
    opened = next((e for e in events if e.kind == RUN_STARTED), None)
    if opened is None:
        raise ValueError("cannot replay: the event stream has no run_started event")
    # Identity is born from the opening fact (run_id/parent/depth/task); the
    # opening state is already a state_delta event in the stream, so nothing is
    # seeded a second time.
    run = Run(
        plan,
        opened.run_id,
        parent_id=opened.parent_id,
        depth=opened.data.get("depth", 0),
        task=opened.data.get("task", ""),
    )
    for ev in events:
        run.apply(ev)  # the single throat: same path the live commits took
    return run
