"""trace — project the event stream into a causal tree of Runs.

Like state and artifacts, a trace is not a second system: every Event already
carries ``run_id``, a monotonic ``seq``, and ``parent_id`` (a child Run's events
point at its parent). Fold those fields and the whole execution — parent and the
sub-agents it delegated to — falls out as a tree:

    build_trace(events) -> nested Span objects (one per Run, children attached)
    render_trace(events) -> an indented, human-readable trace (CLI / teaching)

A Span is one Run: its ordered facts plus the Runs it spawned. This is the
zero-dependency core of observability; exporting the same causal structure to
OpenTelemetry is an adapter that attaches to the Bus (see backends), not a
change here.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from src.kernel.eventlog import (
    ARTIFACT_WRITTEN,
    CONTROL,
    DELEGATED,
    INTERRUPTED,
    NODE_COMPLETED,
    NODE_FAILED,
    NODE_SKIPPED,
    NODE_STARTED,
    RUN_FAILED,
    RUN_STARTED,
)


@dataclass
class Span:
    """One Run as a trace node: identity, its facts, and the Runs it spawned."""

    run_id: str
    name: str
    task: str
    events: list[Any] = field(default_factory=list)
    children: list[Span] = field(default_factory=list)
    start_ts: float = 0.0
    end_ts: float = 0.0
    status: str = "ok"  # ok | error | suspended

    @property
    def label(self) -> str:
        who = self.name or self.run_id
        return f"{who} — {self.task}" if self.task else who

    @property
    def duration_ms(self) -> int:
        return max(0, round((self.end_ts - self.start_ts) * 1000))


def span_to_dict(span: Span) -> dict[str, Any]:
    """JSON-safe view for the web UI (events stay in the Events tab; the trace
    carries identity, status, timing and the spawned children). ``start_ts``/
    ``end_ts`` are the same monotonic clock the Event stream stamps, so a
    client can lay the Run tree out as a time-axis waterfall by subtraction."""
    return {
        "run_id": span.run_id,
        "name": span.name,
        "task": span.task,
        "status": span.status,
        "duration_ms": span.duration_ms,
        "start_ts": span.start_ts,
        "end_ts": span.end_ts,
        "children": [span_to_dict(c) for c in span.children],
    }


def build_trace(events: list[Any]) -> list[Span]:
    """Group a flat (possibly multi-Run) event list into a tree of Spans.

    Parent/child comes from each Run's RUN_STARTED event ``parent_id``; Runs
    whose parent is absent or unknown are returned as roots.
    """
    by_run: dict[str, list[Any]] = {}
    for ev in events:
        by_run.setdefault(ev.run_id, []).append(ev)

    # One pass per Run: sort its facts, read RUN_STARTED a single time
    # (identity, status and the parent), and build the Span — the parent is
    # never re-scanned in a second loop.
    spans: dict[str, Span] = {}
    parents: dict[str, str | None] = {}
    for run_id, evs in by_run.items():
        ordered = sorted(evs, key=lambda e: e.seq)
        started = next((e for e in ordered if e.kind == RUN_STARTED), None)
        parents[run_id] = started.parent_id if started is not None else None
        data = started.data if started is not None else {}
        kinds = {e.kind for e in ordered}
        status = "error" if RUN_FAILED in kinds else "suspended" if INTERRUPTED in kinds else "ok"
        spans[run_id] = Span(
            run_id,
            data.get("name", ""),
            data.get("task", ""),
            ordered,
            [],
            start_ts=ordered[0].ts,
            end_ts=ordered[-1].ts,
            status=status,
        )

    roots: list[Span] = []
    for run_id, span in spans.items():
        parent_id = parents[run_id]
        if parent_id and parent_id in spans:
            spans[parent_id].children.append(span)
        else:
            roots.append(span)
    return roots


def _brief(ev: Any) -> str:
    d = ev.data
    if ev.kind in (NODE_STARTED, NODE_COMPLETED, NODE_SKIPPED):
        return d.get("node", "")
    if ev.kind == NODE_FAILED:
        return f"{d.get('node', '')}  {d.get('error', '')}"
    if ev.kind == CONTROL:
        target = d.get("target") or d.get("template", "")
        return f"{d.get('op', '')} {target}".strip()
    if ev.kind == DELEGATED:
        return f"spawn {d.get('child_run_id', '')}"
    if ev.kind == ARTIFACT_WRITTEN:
        return f"{d.get('filename', '')} v{d.get('version', '')}"
    if ev.kind == INTERRUPTED:
        return f"park {list(d.get('parked', {}))}"
    return ""


def render_trace(events: list[Any]) -> str:
    """Indent Runs and their facts into a readable trace (the CLI's view)."""
    lines: list[str] = []

    def walk(span: Span, depth: int) -> None:
        pad = "  " * depth
        lines.append(f"{pad}● {span.label}")
        for ev in span.events:
            if ev.kind == RUN_STARTED:
                continue  # the Span header already states who this Run is
            brief = _brief(ev)
            lines.append(f"{pad}  {ev.seq:>3} {ev.kind:<17} {brief}".rstrip())
        for child in span.children:
            walk(child, depth + 1)

    for root in build_trace(events):
        walk(root, 0)
    return "\n".join(lines)
