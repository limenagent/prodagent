"""ReplicatedEventLog — the dual-write tier: local truth, async shipping.

The cross-machine topology of the fact pipeline (REPLAY-PLAN U-L4): a
process writes its WAL synchronously to a LOCAL log (fast, survives in-process
reads and recovery on the same host) while a background shipper drains
batches to a SHARED log through ``replicate`` — pre-sequenced, idempotent
absorption, so the shared store's seq space equals the local one and a
checkpoint cursor stays valid on either side.

When the local disk dies with its machine, another machine picks the run
up from the SHARED side: checkpoint base + ``get_after`` tail fold, exactly
``hybrid_restore`` on the shared log — nothing about recovery changes, only
which backend answers it.

Crash window (RPO): events accepted locally but not yet shipped. They are
never dropped — a full ship queue backpressures the producer, and a failing
shared store is retried with backoff forever — but a process kill can still
outrun the shipper. ``flush()`` (or ``aclose()``) drives the window to zero
on demand.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from typing import TYPE_CHECKING

from prodagent.backends._shared.tailing import StreamWakes, tail_stream

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from prodagent.base.event_log import Event
    from prodagent.ports.observability import EventLog

logger = logging.getLogger(__name__)

__all__ = ["ReplicatedEventLog"]

_RETRY_BASE_S = 0.05
_RETRY_MAX_S = 2.0


class ReplicatedEventLog:
    """Dual-write decorator: ``local`` is the synchronous truth this process
    reads; ``remote`` is the shared store another machine recovers from.

    ``maxsize`` bounds the in-flight shipping window (backpressure threshold
    — facts are slowed, never dropped); ``max_batch`` caps one ship's size.
    ``pending()`` is the RPO meter: events accepted minus events shipped.
    """

    def __init__(
        self,
        local: EventLog,
        remote: EventLog,
        *,
        maxsize: int = 1024,
        max_batch: int = 256,
    ) -> None:
        self._local = local
        self._remote = remote
        self._queue: asyncio.Queue[Event] = asyncio.Queue(maxsize=maxsize)
        self._max_batch = max_batch
        self._wakes = StreamWakes()
        self._accepted = 0
        self._shipped = 0
        self._shipped_event = asyncio.Event()
        self._shipped_event.set()  # nothing accepted yet → trivially shipped
        self._task: asyncio.Task[None] | None = None

    # -- lifecycle ---------------------------------------------------------

    async def start(self) -> None:
        """Spawn the shipper. Idempotent; auto-invoked by the first append."""
        if self._task is None:
            self._task = asyncio.create_task(self._ship_loop(), name="replicated-event-log")

    async def aclose(self) -> None:
        """Ship everything accepted, then stop the shipper."""
        await self.flush()
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            self._task = None

    async def flush(self) -> None:
        """Block until every accepted event has been shipped (RPO → 0)."""
        while True:
            if self._shipped >= self._accepted:
                return
            await self._shipped_event.wait()

    def pending(self) -> int:
        """The crash window in events — accepted locally, not yet shared."""
        return self._accepted - self._shipped

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
        # Local is the synchronous truth: it assigns seqs (its optimistic
        # concurrency guards the stream) and answers this process's reads.
        seqs = await self._local.append_events(events, expected_seq)
        self._accepted += len(events)
        self._shipped_event.clear()
        for stream_id in {e.stream_id for e in events}:
            self._wakes.notify(stream_id)
        # Outside any lock: a full shipping queue backpressures this
        # coroutine — that is the contract, never dropping facts.
        for event in events:
            await self._queue.put(event)
        return seqs

    async def get_events(self, stream_id: str) -> list[Event]:
        return await self._local.get_events(stream_id)

    async def get_after(self, stream_id: str, since_seq: int) -> list[Event]:
        return await self._local.get_after(stream_id, since_seq)

    def subscribe(self, stream_id: str, since_seq: int = 0) -> AsyncIterator[Event]:
        return tail_stream(self.get_after, self._wakes, stream_id, since_seq)

    async def replicate(self, events: list[Event]) -> None:
        """Pass through — a replicated tier can itself be replicated."""
        await self._local.replicate(events)

    # -- internals -----------------------------------------------------------

    async def _ship_loop(self) -> None:
        while True:
            batch = [await self._queue.get()]
            while len(batch) < self._max_batch:
                try:
                    batch.append(self._queue.get_nowait())
                except asyncio.QueueEmpty:
                    break
            await self._ship_with_retry(batch)
            self._shipped += len(batch)
            if self._shipped >= self._accepted:
                self._shipped_event.set()

    async def _ship_with_retry(self, batch: list[Event]) -> None:
        """Ship one batch; a failing shared store is retried with backoff —
        the batch is held in hand until it lands, so failures extend the RPO
        window but never lose facts."""
        delay = _RETRY_BASE_S
        while True:
            try:
                await self._remote.replicate(batch)
                return
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 — the shipper must outlive remote outages
                logger.exception(
                    "[replicated] shipping %d events failed — retrying in %.2fs",
                    len(batch),
                    delay,
                )
                await asyncio.sleep(delay)
                delay = min(delay * 2, _RETRY_MAX_S)
