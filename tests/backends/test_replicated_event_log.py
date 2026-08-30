"""Laws for ``ReplicatedEventLog`` — the dual-write tier of U-L4.

1. Suffix-transfer law: after flush, the shared store answers ``get_after``
   exactly as the local one does, for every cursor — the cross-machine
   recovery primitive (a checkpoint cursor written against local seqs stays
   valid against the shared side).
2. Idempotent healing: re-shipping a batch adds nothing.
3. RPO law: ``pending()`` counts the unshipped window and flush drives it
   to zero; a full queue backpressures instead of dropping.
4. Takeover law (the acceptance test): a second "machine" reading only the
   shared side folds the same state the crashed process had.
5. Outage law: a shared store that fails is retried, never skipped.
"""

from __future__ import annotations

import asyncio
from typing import Any

from prodagent.backends.memory.event_log import InMemoryEventLog
from prodagent.backends.replicated import ReplicatedEventLog
from prodagent.base.event_log import Event, PlanEventType


def _event(stream_id: str, version: int, **data: Any) -> Event:
    return Event.make(PlanEventType.STEP_COMPLETED, stream_id, version, **data)


async def _write(log: ReplicatedEventLog, stream: str, n: int, start: int = 1) -> list[Event]:
    events = [_event(stream, v, step_id=f"s{v}") for v in range(start, start + n)]
    await log.append_events(events)
    return events


async def test_suffix_transfer_law() -> None:
    local, shared = InMemoryEventLog(), InMemoryEventLog()
    log = ReplicatedEventLog(local, shared)
    await _write(log, "r1", 5)
    await log.flush()

    # Every cursor answers identically on both sides — a checkpoint written
    # against local seqs resumes correctly from the shared store.
    for cursor in range(0, 6):
        assert [e.seq for e in await shared.get_after("r1", cursor)] == [
            e.seq for e in await local.get_after("r1", cursor)
        ], f"suffix diverged at cursor {cursor}"
    assert [e.event_id for e in await shared.get_events("r1")] == [
        e.event_id for e in await local.get_events("r1")
    ]
    await log.aclose()


async def test_replicate_is_idempotent_healing() -> None:
    shared = InMemoryEventLog()
    events = [_event("r1", v) for v in range(1, 4)]
    for i, event in enumerate(events, start=1):
        event.seq = i  # sequenced by the source's append before shipping
    await shared.replicate(events)
    await shared.replicate(events)  # the crash-then-reship case
    assert [e.seq for e in await shared.get_events("r1")] == [1, 2, 3]


async def test_replicate_rejects_unsequenced_events() -> None:
    """A seq=0 placeholder was never sequenced by a source — replicate
    refuses loudly instead of silently dropping a fact."""
    import pytest

    shared = InMemoryEventLog()
    with pytest.raises(ValueError, match="unsequenced"):
        await shared.replicate([_event("r1", 1)])


async def test_rpo_law_window_counts_and_flush_drives_to_zero() -> None:
    released = asyncio.Event()

    class GatedShared(InMemoryEventLog):
        async def replicate(self, events, expected_seq=None):  # type: ignore[no-untyped-def]
            await released.wait()
            return await super().replicate(events)

    local, shared = InMemoryEventLog(), GatedShared()
    log = ReplicatedEventLog(local, shared, maxsize=8, max_batch=8)
    await _write(log, "r1", 6)
    assert log.pending() > 0, "unshipped events are the RPO window"

    released.set()
    await log.flush()
    assert log.pending() == 0
    assert len(await shared.get_events("r1")) == 6
    await log.aclose()


async def test_takeover_another_machine_recovers_from_shared_side() -> None:
    """The U-L4 acceptance test: process A writes locally + ships; process A
    dies; process B — seeing only the shared store and the checkpoint —
    folds the same state."""
    from prodagent.base.event_log import hybrid_restore

    local, shared = InMemoryEventLog(), InMemoryEventLog()
    log = ReplicatedEventLog(local, shared)
    await _write(log, "r1", 4)
    await log.flush()  # ship before the crash — RPO zero at kill time
    # No aclose on purpose: the process "died" after shipping.

    # Machine B: fold purely from the shared side — the same hybrid_restore
    # recovery uses, unchanged, on a different backend instance.
    from prodagent.backends.factory import in_memory_checkpoint_store

    def reducer(state: list[str], event: Event) -> None:
        state.append(event.data.get("step_id", "?"))

    state, _version, last_seq = await hybrid_restore(
        "r1",
        shared,  # the shared store, not A's local
        in_memory_checkpoint_store(),  # B holds no checkpoint for this run — full fold
        reducer,
        extract_base=lambda run: None,
        empty_state=list,
    )
    assert state == ["s1", "s2", "s3", "s4"]
    assert last_seq == 4


async def test_outage_law_retries_instead_of_skipping() -> None:
    class FlakyShared(InMemoryEventLog):
        def __init__(self) -> None:
            super().__init__()
            self.failures_left = 1

        async def replicate(self, events, expected_seq=None):  # type: ignore[no-untyped-def]
            if self.failures_left > 0:
                self.failures_left -= 1
                raise ConnectionError("shared store unreachable")
            return await super().replicate(events)

    local, shared = InMemoryEventLog(), FlakyShared()
    log = ReplicatedEventLog(local, shared)
    await _write(log, "r1", 2)
    await log.flush()  # must ride out the one outage, not lose the batch
    assert [e.seq for e in await shared.get_events("r1")] == [1, 2]
    await log.aclose()


async def test_reads_and_subscribe_serve_local() -> None:
    import contextlib

    local, shared = InMemoryEventLog(), InMemoryEventLog()
    log = ReplicatedEventLog(local, shared)
    await _write(log, "r1", 2)

    gen = log.subscribe("r1", since_seq=0)
    got: list[Event] = []

    async def _collect() -> None:
        async for event in gen:
            got.append(event)

    task = asyncio.create_task(_collect())
    await asyncio.sleep(0.15)
    await log.append(_event("r1", 3, step_id="s3"))
    await asyncio.sleep(0.15)
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task
    await gen.aclose()
    await log.aclose()
    assert [e.seq for e in got] == [1, 2, 3]
