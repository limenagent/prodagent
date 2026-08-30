"""In-process event log — the bare-profile default.

Keeps PLAN_FIRST execution working with zero disk footprint: state dies with
the process; cross-restart durability is the production profile's
``FileEventLog`` (or Postgres).
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

from prodagent.backends._shared.tailing import StreamWakes, tail_stream
from prodagent.base.errors import VersionConflict

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from prodagent.base.event_log import Event

__all__ = ["InMemoryEventLog"]


class InMemoryEventLog:
    """Append-only event log in process memory — per-stream monotonic seq.

    Mirrors :class:`~prodagent.backends.file.event_log.FileEventLog`
    semantics: optimistic concurrency via ``expected_seq``, events kept in
    append order per ``stream_id``.
    """

    def __init__(self) -> None:
        self._streams: dict[str, list[Event]] = {}
        self._lock = asyncio.Lock()
        self._wakes = StreamWakes()

    async def append(self, event: Event, expected_seq: int | None = None) -> int:
        return (await self.append_events([event], expected_seq))[0]

    async def append_events(
        self, events: list[Event], expected_seq: int | None = None
    ) -> list[int]:
        if not events:
            return []
        # One lock pass for the whole batch: consecutive seqs per stream and a
        # single wake afterwards — subscribers see the batch atomically.
        async with self._lock:
            checked: set[str] = set()
            for event in events:
                stream_events = self._streams.setdefault(event.stream_id, [])
                current = stream_events[-1].seq if stream_events else 0
                if event.stream_id not in checked:
                    if expected_seq is not None and current != expected_seq:
                        raise VersionConflict(
                            f"expected tail seq {expected_seq} for stream {event.stream_id}, "
                            f"found {current} — concurrent writer won"
                        )
                    checked.add(event.stream_id)
                event.seq = current + 1
                stream_events.append(event)
            for stream_id in checked:
                self._wakes.notify(stream_id)
            return [e.seq for e in events]

    async def get_events(self, stream_id: str) -> list[Event]:
        async with self._lock:
            return list(self._streams.get(stream_id, []))

    async def get_after(self, stream_id: str, since_seq: int) -> list[Event]:
        async with self._lock:
            return [e for e in self._streams.get(stream_id, []) if e.seq > since_seq]

    def subscribe(self, stream_id: str, since_seq: int = 0) -> AsyncIterator[Event]:
        return tail_stream(self.get_after, self._wakes, stream_id, since_seq)

    async def replicate(self, events: list[Event]) -> None:
        """Absorb at the events' own seqs — skip what the tail already
        covers, append the rest in order. Sparse tails are tolerated (reads
        order by seq); duplicates heal instead of duplicating."""
        if not events:
            return
        for event in events:
            if event.seq < 1:
                # Event.make leaves seq=0 as the "unassigned" placeholder —
                # shipping one means the source never sequenced it (a wiring
                # bug), and silently skipping it would lose a fact.
                raise ValueError(
                    f"cannot replicate an unsequenced event ({event.event_type} on "
                    f"{event.stream_id}) — append it through a log first"
                )
        async with self._lock:
            tails: dict[str, int] = {}
            for event in events:
                stream_events = self._streams.setdefault(event.stream_id, [])
                tail = tails.get(event.stream_id)
                if tail is None:
                    tail = stream_events[-1].seq if stream_events else 0
                    tails[event.stream_id] = tail
                if event.seq <= tail:
                    continue  # already absorbed — idempotent re-ship
                stream_events.append(event)
                tails[event.stream_id] = event.seq
            for stream_id in tails:
                self._wakes.notify(stream_id)
