"""Laws for ``BufferedEventLog`` — the write-behind throughput tier.

Three laws, one per promise the decorator makes:

1. Order law — after ``aclose()``, the wrapped log holds every accepted
   event in acceptance order with consecutive seqs (group commit changes
   write batching, never ordering).
2. Merged-view law — ``get_after`` observes accepted events *immediately*
   (before drain), with no duplicates and no gaps even while batches are
   in flight.
3. Backpressure law — a full queue slows the producer, never drops or
   reorders; releasing the drain lets everything through in order.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from prodagent.backends.buffered import BufferedEventLog
from prodagent.backends.memory.event_log import InMemoryEventLog
from prodagent.base.errors import VersionConflict
from prodagent.base.event_log import Event, PlanEventType


def _event(stream_id: str, version: int, **data: Any) -> Event:
    return Event.make(PlanEventType.STEP_COMPLETED, stream_id, version, **data)


async def test_order_law_after_close(tmp_path: Any) -> None:
    inner = InMemoryEventLog()
    log = BufferedEventLog(inner)
    ids = []
    for i in range(25):
        event = _event("s1", i, step_id=f"s{i}")
        ids.append(event.event_id)
        await log.append(event)
    await log.aclose()

    stored = await inner.get_events("s1")
    assert [e.event_id for e in stored] == ids, "acceptance order preserved"
    assert [e.seq for e in stored] == list(range(1, 26)), "seqs consecutive from 1"
    assert await log.get_events("s1") == stored, "merged view equals committed view after drain"


async def test_merged_view_law_before_flush() -> None:
    inner = InMemoryEventLog()
    log = BufferedEventLog(inner, max_batch=4)
    for i in range(6):
        await log.append(_event("s1", i, step_id=f"s{i}"))

    # No flush, no close: the merged read must already see every accepted
    # event, exactly once, gapless — the suffix law holds at every instant.
    view = await log.get_after("s1", 0)
    assert [e.seq for e in view] == [1, 2, 3, 4, 5, 6]

    # While drains race the reader, every observation stays dup-free/gapless.
    for _ in range(10):
        for i in range(6, 12):
            await log.append(_event("s1", i, step_id=f"s{i}"))
        seen = [e.seq for e in await log.get_after("s1", 0)]
        assert seen == sorted(set(seen)), "no duplicates under drain race"
        assert seen == list(range(1, len(seen) + 1)), "no gaps under drain race"
    await log.aclose()


async def test_backpressure_law_blocks_then_releases() -> None:
    released = asyncio.Event()

    class GatedLog(InMemoryEventLog):
        async def append_events(self, events, expected_seq=None):  # type: ignore[no-untyped-def]
            await released.wait()
            return await super().append_events(events, expected_seq)

    inner = GatedLog()
    log = BufferedEventLog(inner, maxsize=2, max_batch=8)

    appends = [asyncio.create_task(log.append(_event("s1", i, step_id=f"s{i}"))) for i in range(6)]
    await asyncio.sleep(0.2)
    # Gate closed: the drain holds its batch, the queue fills, producers block
    # — blocked is exactly the contract (facts are slowed, never dropped).
    pending = [t for t in appends if not t.done()]
    assert len(pending) > 0, "full queue must backpressure the producer"

    released.set()
    seqs = await asyncio.gather(*appends)
    assert seqs == [1, 2, 3, 4, 5, 6], "release lets everything through in order"
    await log.aclose()
    assert [e.seq for e in await inner.get_events("s1")] == [1, 2, 3, 4, 5, 6]


async def test_expected_seq_checked_against_merged_tail() -> None:
    inner = InMemoryEventLog()
    log = BufferedEventLog(inner)
    await log.append(_event("s1", 1))
    with pytest.raises(VersionConflict):
        await log.append(_event("s1", 2), expected_seq=0)
    await log.aclose()
    assert len(await inner.get_events("s1")) == 1


async def test_flush_closes_the_window() -> None:
    inner = InMemoryEventLog()
    log = BufferedEventLog(inner, max_batch=2)
    for i in range(5):
        await log.append(_event("s1", i, step_id=f"s{i}"))
    await log.flush()
    assert len(await inner.get_events("s1")) == 5, "flush drains everything accepted"


async def test_subscribe_lives_through_the_buffer() -> None:
    import contextlib

    log = BufferedEventLog(InMemoryEventLog())
    await log.append(_event("s1", 1))

    gen = log.subscribe("s1", since_seq=0)
    got: list[Event] = []

    async def _collect() -> None:
        async for event in gen:
            got.append(event)

    task = asyncio.create_task(_collect())
    await asyncio.sleep(0.15)
    await log.append(_event("s1", 2))
    await log.append(_event("s1", 3))
    await asyncio.sleep(0.15)
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task
    await gen.aclose()
    await log.aclose()
    assert [e.seq for e in got] == [1, 2, 3]
