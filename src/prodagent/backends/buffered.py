"""BufferedEventLog — the write-behind throughput tier for the fact pipeline.

Appends land on a bounded queue (a full queue backpressures the producer —
facts are never dropped, only slowed); a background task drains batches to
the wrapped log through ``append_events`` (one physical write per batch —
group commit). Reads serve the *merged* view — wrapped events plus the
not-yet-drained tail — so the suffix law holds at every instant, not just
after flush: a reader never sees a gap while a batch is in flight, and
never sees an event twice (pending entries with ``seq`` at or below what
the wrapped log already returned are filtered out).

Crash window: events accepted but not yet drained die with the process.
That bounded window is the documented RPO of this tier — ``flush()`` (or
``aclose()``) drains it, and any consumer that must not observe it
(recovery, REPLAY/SIM) flushes first or reads the wrapped log directly.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from typing import TYPE_CHECKING

from prodagent.backends._shared.tailing import StreamWakes, tail_stream
from prodagent.base.errors import VersionConflict

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from prodagent.base.event_log import Event
    from prodagent.ports.observability import EventLog

logger = logging.getLogger(__name__)

__all__ = ["BufferedEventLog"]


class BufferedEventLog:
    """Bounded-channel decorator over any ``EventLog``.

    ``maxsize`` bounds the in-flight window (the backpressure threshold);
    ``max_batch`` caps one drain's group-commit size; ``linger`` optionally
    holds the first arrival briefly so a burst coalesces into one batch
    (the interval tier of the fsync policy — the file backend's ``fsync``
    flag is the every-call tier, and one drain = one append_events call is
    the batch tier).
    """

    def __init__(
        self,
        inner: EventLog,
        *,
        maxsize: int = 1024,
        max_batch: int = 128,
        linger: float = 0.0,
    ) -> None:
        self._inner = inner
        self._queue: asyncio.Queue[Event] = asyncio.Queue(maxsize=maxsize)
        self._max_batch = max_batch
        self._linger = linger
        self._lock = asyncio.Lock()
        self._wakes = StreamWakes()
        # Merged per-stream tails (committed ∪ pending) — the seq mint and
        # the expected_seq check both run against this, never the inner tail
        # alone, so ordering holds whether or not a batch has drained yet.
        self._tails: dict[str, int] = {}
        self._pending: dict[str, list[Event]] = {}
        self._accepted = 0
        self._written = 0
        self._drained = asyncio.Event()
        self._drained.set()  # nothing accepted yet → trivially drained
        self._task: asyncio.Task[None] | None = None

    # -- lifecycle ---------------------------------------------------------

    async def start(self) -> None:
        """Spawn the drain task. Idempotent; also auto-invoked lazily by the
        first append, so callers that never call it still get a working log
        (explicit start is for wiring that wants fail-fast startup)."""
        if self._task is None:
            self._task = asyncio.create_task(self._drain_loop(), name="buffered-event-log")

    async def aclose(self) -> None:
        """Drain everything accepted, then stop the background task."""
        await self.flush()
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            self._task = None

    async def flush(self) -> None:
        """Block until every accepted event has been handed to the wrapped
        log (``written >= accepted``) — close the crash window on demand."""
        while True:
            async with self._lock:
                if self._written >= self._accepted:
                    return
            # Woken by the drain task; re-check under the lock on return —
            # concurrent appends keep re-clearing the event, which just
            # means flush keeps chasing the newest accepted count.
            await self._drained.wait()

    # -- EventLog surface --------------------------------------------------

    async def append(self, event: Event, expected_seq: int | None = None) -> int:
        return (await self.append_events([event], expected_seq))[0]

    async def append_events(
        self, events: list[Event], expected_seq: int | None = None
    ) -> list[int]:
        if not events:
            return []
        if self._task is None:
            await self.start()
        async with self._lock:
            checked: set[str] = set()
            for event in events:
                tail = await self._merged_tail(event.stream_id)
                # Same contract as the bare backends: checked against the
                # merged tail before the first event of each involved stream.
                if expected_seq is not None and event.stream_id not in checked:
                    if tail != expected_seq:
                        raise VersionConflict(
                            f"expected tail seq {expected_seq} for stream {event.stream_id}, "
                            f"found {tail} — concurrent writer won"
                        )
                    checked.add(event.stream_id)
                event.seq = tail + 1
                self._tails[event.stream_id] = event.seq
                self._pending.setdefault(event.stream_id, []).append(event)
                self._accepted += 1
            self._drained.clear()
        # Outside the lock: a full queue must backpressure *this* coroutine,
        # not block readers. FIFO order is the queue's guarantee.
        for event in events:
            await self._queue.put(event)
        for stream_id in {e.stream_id for e in events}:
            self._wakes.notify(stream_id)
        return [e.seq for e in events]

    async def get_events(self, stream_id: str) -> list[Event]:
        committed = await self._inner.get_events(stream_id)
        committed_max = committed[-1].seq if committed else 0
        async with self._lock:
            pending = [e for e in self._pending.get(stream_id, []) if e.seq > committed_max]
        return committed + pending

    async def get_after(self, stream_id: str, since_seq: int) -> list[Event]:
        committed = await self._inner.get_after(stream_id, since_seq)
        committed_max = committed[-1].seq if committed else 0
        async with self._lock:
            pending = [e for e in self._pending.get(stream_id, []) if e.seq > committed_max]
        return committed + pending

    def subscribe(self, stream_id: str, since_seq: int = 0) -> AsyncIterator[Event]:
        return tail_stream(self.get_after, self._wakes, stream_id, since_seq)

    # -- internals -----------------------------------------------------------

    async def _merged_tail(self, stream_id: str) -> int:
        """Committed ∪ pending tail for ``stream_id``. Inner tail is fetched
        once per stream (then cached in ``_tails``) — the only O(stream)
        startup cost, amortized to zero on the hot path."""
        if stream_id not in self._tails:
            events = await self._inner.get_events(stream_id)
            self._tails[stream_id] = events[-1].seq if events else 0
        return self._tails[stream_id]

    async def _drain_loop(self) -> None:
        while True:
            batch = [await self._queue.get()]
            if self._linger > 0:
                await asyncio.sleep(self._linger)
            while len(batch) < self._max_batch:
                try:
                    batch.append(self._queue.get_nowait())
                except asyncio.QueueEmpty:
                    break
            await self._inner.append_events(batch)
            # Drop drained events from the merged view *after* the inner log
            # has them — between commit and this removal a reader may see an
            # event in both places, which the seq filter in get_after/get_events
            # collapses (that ordering is why reads dedupe by committed_max).
            async with self._lock:
                for event in batch:
                    pending = self._pending.get(event.stream_id)
                    if pending is not None:
                        pending.remove(event)  # event_id (uuid4) is unique per event
                self._written += len(batch)
                if self._written >= self._accepted:
                    self._drained.set()
