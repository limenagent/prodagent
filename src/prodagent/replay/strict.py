"""STRICT — the equivalence comparator behind the replay law.

The law: ``strict(replay(cassette(run))) ≡ run`` — re-enacting a run from
its tape and comparing against the original must find no difference. The
comparison runs on TWO projections, never raw objects:

- the **event-flow projection**: the decision flow (turn markers, tool
  calls and results, the terminal event), with presentation-layer streams
  (think-token text) and naturally-volatile fields (ids, timestamps,
  latencies) projected away;
- the **terminal projection**: how the run ended — state, final output,
  turn count, tool history, error.

Projections exist because a naive compare would cry wolf: two executions
of the same run never share event ids or wall-clock stamps, and a
comparator that fails on them gets muted. Everything semantic stays in:
tool names, arguments, results, outcomes, the words of the final answer.
"""

from __future__ import annotations

import dataclasses
from typing import Any

__all__ = [
    "ReplayNotEquivalent",
    "event_flow_projection",
    "strict_compare",
    "terminal_projection",
]

# Fields that differ between any two executions of the same run, by nature:
# identity stamps and clocks. Everything else is semantic and compared.
_VOLATILE_KEYS = frozenset(
    {
        "run_id",
        "parent_run_id",
        "event_id",
        "seq",
        "span_id",
        "trace_id",
        "parent_span_id",
        "timestamp",
        "created_at",
        "decided_at",
        "last_access",
        "latency_ms",
        "elapsed_seconds",
        "session_id",
    }
)

# Event types whose payload is presentation, not decisions: the streamed
# think-token text. The reasoning itself is compared where it steers the
# run — via the taped responses — not as a token stream.
_PRESENTATION_EVENTS = frozenset({"ThinkTokenEvent"})


class ReplayNotEquivalent(Exception):
    """The replay diverged from the original — raised carrying every
    divergence found, first difference first."""


def _strip_volatile(value: Any) -> Any:
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        return _strip_volatile(dataclasses.asdict(value))
    if isinstance(value, dict):
        return {k: _strip_volatile(v) for k, v in value.items() if k not in _VOLATILE_KEYS}
    if isinstance(value, list):
        return [_strip_volatile(v) for v in value]
    if isinstance(value, tuple):
        return [_strip_volatile(v) for v in value]
    return value


def project_event(event: Any) -> dict[str, Any] | None:
    """One event as its comparable essence, or ``None`` to skip it."""
    name = type(event).__name__
    if name in _PRESENTATION_EVENTS:
        return None
    # Terminal events carry the whole run; the run is compared once, as the
    # terminal projection — here only the ending's shape matters.
    if hasattr(event, "run"):
        return {"__event__": name, "error": getattr(event, "error", None)}
    projected = _strip_volatile(dataclasses.asdict(event))
    return {"__event__": name, **projected}


def event_flow_projection(events: list[Any]) -> list[dict[str, Any]]:
    """A run's decision flow as comparable essences, in order."""
    return [p for p in (project_event(e) for e in events) if p is not None]


def terminal_projection(run: Any) -> dict[str, Any]:
    """How the run ended — the second half of the double assertion."""
    return {
        "state": str(run.state),
        "final_output": run.final_output,
        "turn_count": run.turn_count,
        "tool_history": [{"name": c.name, "params": dict(c.params)} for c in run.tool_history],
        "error": str(run.last_error) if run.last_error else None,
    }


def strict_compare(
    live_run: Any,
    live_events: list[Any],
    replay_run: Any,
    replay_events: list[Any],
) -> list[str]:
    """Every divergence between the original and the re-enactment, in
    order; empty means equivalent. Does not raise — callers (and the law)
    decide what an unempty list means."""
    diffs: list[str] = []
    live_flow = event_flow_projection(live_events)
    replay_flow = event_flow_projection(replay_events)
    for i in range(max(len(live_flow), len(replay_flow))):
        a = live_flow[i] if i < len(live_flow) else None
        b = replay_flow[i] if i < len(replay_flow) else None
        if a != b:
            diffs.append(f"event flow diverges at position {i + 1}: {a!r} vs {b!r}")
            break  # the first divergence is the diagnosis; the rest is echo

    live_terminal = terminal_projection(live_run)
    replay_terminal = terminal_projection(replay_run)
    for key in live_terminal:
        if live_terminal[key] != replay_terminal[key]:
            diffs.append(
                f"terminal projection diverges on {key}: "
                f"{live_terminal[key]!r} vs {replay_terminal[key]!r}"
            )
    return diffs


def assert_equivalent(
    live_run: Any,
    live_events: list[Any],
    replay_run: Any,
    replay_events: list[Any],
) -> None:
    """The law as a call: raise :class:`ReplayNotEquivalent` (or pass
    through a :class:`CassetteMismatch` untouched — a tape that cannot
    answer is a harder failure than a divergence) when they differ."""
    diffs = strict_compare(live_run, live_events, replay_run, replay_events)
    if diffs:
        raise ReplayNotEquivalent("; ".join(diffs))
