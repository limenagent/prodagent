"""eventlog — events are the source of truth; state is a projection folded
from the event stream.

Three pieces:
- Event: an immutable "fact that happened", append-only, never modified;
- apply_event: a pure function that folds one event into shared state —
  replaying the stream rebuilds state;
- EventLog / CheckpointStore storage protocols plus in-process defaults; in
  production swap in Redis/Postgres.

Why fold state from events instead of storing one latest dict? Because the
event stream gives you audit (how we got here), time travel (back to any
step), and crash recovery (just replay) all at once.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol

from src.kernel.channels import Channel

# Event kinds (the teaching build keeps just the most telling ones;
# production can be finer-grained).
RUN_STARTED = "run_started"
NODE_STARTED = "node_started"
NODE_COMPLETED = "node_completed"
NODE_RETRY = "node_retry"
STATE_DELTA = "state_delta"
INTERRUPTED = "interrupted"
RESUMED = "resumed"
RUN_COMPLETED = "run_completed"
RUN_FAILED = "run_failed"


@dataclass(frozen=True)
class Event:
    seq: int  # monotonically increasing within a run
    run_id: str
    kind: str
    data: dict[str, Any] = field(default_factory=dict)
    parent_id: str | None = None  # child-Run events attach to the parent to rebuild the Run tree


def apply_event(shared: dict[str, Any], event: Event, channels: dict[str, Channel]) -> None:
    """Fold a single event into ``shared`` (in place). Pure: the same event
    stream always yields the same state."""
    if event.kind != STATE_DELTA:
        return
    for key, value in event.data.get("delta", {}).items():
        channel = channels[key]
        shared[key] = channel.fold(shared.get(key), value)


def fold_events(
    events: list[Event], channels: dict[str, Channel], initial: dict[str, Any]
) -> dict[str, Any]:
    """Replay a whole event stream from an initial state (tests, time travel)."""
    shared = dict(initial)
    for ev in events:
        apply_event(shared, ev, channels)
    return shared


class EventLog(Protocol):
    async def append(self, event: Event) -> int: ...
    async def events(self, run_id: str) -> list[Event]: ...
    async def after(self, run_id: str, since_seq: int) -> list[Event]: ...


class CheckpointStore(Protocol):
    async def save(
        self, run_id: str, snapshot: dict, *, expected_version: int | None = None
    ) -> int: ...
    async def load(self, run_id: str) -> dict | None: ...


class InMemoryEventLog:
    """In-process event log, the default implementation; its interface is the
    contract a production backend must satisfy."""

    def __init__(self) -> None:
        self._streams: dict[str, list[Event]] = {}

    async def append(self, event: Event) -> int:
        stream = self._streams.setdefault(event.run_id, [])
        stream.append(event)
        return event.seq

    async def events(self, run_id: str) -> list[Event]:
        return list(self._streams.get(run_id, ()))

    async def after(self, run_id: str, since_seq: int) -> list[Event]:
        return [e for e in self._streams.get(run_id, ()) if e.seq > since_seq]


class InMemoryStore:
    """In-process checkpoint store, the default; swapping in a database only
    touches this layer."""

    def __init__(self) -> None:
        self._snapshots: dict[str, dict] = {}
        self._version: dict[str, int] = {}

    async def save(
        self, run_id: str, snapshot: dict, *, expected_version: int | None = None
    ) -> int:
        # Optimistic concurrency: if an expected version is given it must match,
        # so two executions cannot overwrite each other.
        if expected_version is not None and self._version.get(run_id, 0) != expected_version:
            raise RuntimeError(f"checkpoint version conflict: expected {expected_version}")
        version = self._version.get(run_id, 0) + 1
        self._snapshots[run_id] = snapshot
        self._version[run_id] = version
        return version

    async def load(self, run_id: str) -> dict | None:
        snap = self._snapshots.get(run_id)
        return None if snap is None else dict(snap)

    def version_of(self, run_id: str) -> int:
        return self._version.get(run_id, 0)
