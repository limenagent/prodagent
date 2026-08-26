"""In-process event log — the bare-profile default.

Keeps PLAN_FIRST execution working with zero disk footprint: state dies with
the process; cross-restart durability is the production profile's
``FileEventLog`` (or Postgres).
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

from prodagent.core.exceptions import VersionConflict

if TYPE_CHECKING:
    from prodagent.core.event_log import Event

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

    async def append(self, event: Event, expected_seq: int | None = None) -> int:
        async with self._lock:
            events = self._streams.setdefault(event.stream_id, [])
            current = events[-1].seq if events else 0
            if expected_seq is not None and current != expected_seq:
                raise VersionConflict(
                    f"expected tail seq {expected_seq} for stream {event.stream_id}, "
                    f"found {current} — concurrent writer won"
                )
            event.seq = current + 1
            events.append(event)
            return event.seq

    async def get_events(self, stream_id: str) -> list[Event]:
        async with self._lock:
            return list(self._streams.get(stream_id, []))

    async def get_after(self, stream_id: str, since_seq: int) -> list[Event]:
        async with self._lock:
            return [e for e in self._streams.get(stream_id, []) if e.seq > since_seq]
