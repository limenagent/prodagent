"""Conformance tests for ``EventLog`` implementations."""

from __future__ import annotations

from collections.abc import Callable
from typing import TypeAlias

from prodagent.base.event_log import Event, PlanEventType
from prodagent.ports.observability import EventLog

Factory: TypeAlias = Callable[[], EventLog]


def _event(stream_id: str, version: int, **data: object) -> Event:
    return Event.make(PlanEventType.STEP_COMPLETED, stream_id, version, **data)


async def run_event_log_conformance(make_store: Factory) -> None:
    store = make_store()

    e1 = _event("p1", 1, step_id="s1")
    e2 = _event("p1", 2, step_id="s2")
    seq1 = await store.append(e1)
    seq2 = await store.append(e2)
    assert seq2 > seq1, "append must return monotonic seq"

    events = await store.get_events("p1")
    assert [e.event_id for e in events] == [e1.event_id, e2.event_id]
    assert [e.seq for e in events] == [seq1, seq2]

    tail = await store.get_after("p1", seq1)
    assert [e.event_id for e in tail] == [e2.event_id]
    assert tail[0].seq > seq1


async def run_event_log_plan_isolation_conformance(make_store: Factory) -> None:
    """Events for one stream_id do not leak into another."""
    store = make_store()
    await store.append(_event("pa", 1))
    await store.append(_event("pb", 1))
    await store.append(_event("pa", 2))

    pa = await store.get_events("pa")
    pb = await store.get_events("pb")
    assert len(pa) == 2
    assert len(pb) == 1
    assert {e.stream_id for e in pa} == {"pa"}
    assert {e.stream_id for e in pb} == {"pb"}


async def run_event_log_empty_plan_conformance(make_store: Factory) -> None:
    store = make_store()
    assert await store.get_events("nope") == []
    assert await store.get_after("nope", 0) == []


async def run_event_log_batch_conformance(make_store: Factory) -> None:
    """``append_events`` — the batch-order law: one batch call is
    indistinguishable from the same events appended one by one."""
    store = make_store()
    events = [_event("b1", i) for i in range(1, 4)]
    seqs = await store.append_events(events)
    assert seqs == sorted(seqs) and len(set(seqs)) == 3, "batch seqs must be strictly increasing"

    read = await store.get_events("b1")
    assert [e.event_id for e in read] == [e.event_id for e in events], "batch order preserved"
    assert [e.seq for e in read] == seqs

    # Compare against sequential appends on a *different stream of the same
    # store* — factories may share a directory, so a second store could see
    # the first one's events; a fresh stream cannot.
    sequential = [await store.append(_event("b1-seq", i)) for i in range(1, 4)]
    assert sequential == seqs, "batch and sequential appends assign identical seqs"


async def run_event_log_batch_expected_seq_conformance(make_store: Factory) -> None:
    """Optimistic concurrency is not weakened by batching: a stale tail
    rejects the whole batch, and nothing half-lands."""
    import pytest

    from prodagent.base.errors import VersionConflict

    store = make_store()
    await store.append(_event("c1", 1))
    with pytest.raises(VersionConflict):
        await store.append_events([_event("c1", 2), _event("c1", 3)], expected_seq=0)
    remaining = await store.get_events("c1")
    assert len(remaining) == 1, "a rejected batch must not leave partial writes"


async def run_event_log_subscribe_conformance(make_store: Factory) -> None:
    """``subscribe`` — the suffix law: the subscriber sees exactly
    ``get_after(since_seq)`` plus everything appended afterwards, strictly
    increasing, no duplicates, no gaps."""
    import asyncio
    import contextlib

    store = make_store()
    e1 = await store.append(_event("t1", 1))

    gen = store.subscribe("t1", since_seq=0)
    got: list[Event] = []

    async def _collect() -> None:
        async for event in gen:
            got.append(event)

    task = asyncio.create_task(_collect())
    await asyncio.sleep(0.15)  # drain the pre-existing suffix first
    e2 = await store.append(_event("t1", 2))
    e3 = await store.append(_event("t1", 3))
    await asyncio.sleep(0.15)  # live tail via wake (or one poll interval)
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task
    await gen.aclose()

    assert [e.seq for e in got] == [e1, e2, e3], "suffix must be exact, ordered, gapless"

    # since_seq is respected on a fresh subscription: from e2, only e3 shows.
    gen2 = store.subscribe("t1", since_seq=e2)
    got2: list[Event] = []

    async def _collect2() -> None:
        async for event in gen2:
            got2.append(event)

    task2 = asyncio.create_task(_collect2())
    await asyncio.sleep(0.15)
    task2.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task2
    await gen2.aclose()
    assert [e.seq for e in got2] == [e3]
