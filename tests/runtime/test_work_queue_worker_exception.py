"""Work queue — worker resilience: a worker whose ``try_claim_and_run`` raises
is isolated from the rest of the queue (treated as idle for that round), not
allowed to propagate to ``asyncio.gather`` and kill the whole stream."""

from __future__ import annotations

import pytest

from prodagent.runtime.coordination.termination import MaxRounds, TerminationPolicy
from prodagent.runtime.coordination.work_queue import (
    ItemCompletedEvent,
    QueueDrainedEvent,
    SharedQueue,
    WorkItem,
    WorkQueueSpec,
    WorkResult,
    work_queue_stream,
)


class _AlwaysRaisesWorker:
    """Always raises — never claims. Proves a bad worker is quarantined, not fatal."""

    def __init__(self, name: str) -> None:
        self.name = name
        self.raise_count = 0

    async def try_claim_and_run(self, queue: SharedQueue, *, name: str) -> WorkResult | None:
        self.raise_count += 1
        raise RuntimeError(f"worker {name} exploded")


class _EagerWorker:
    def __init__(self, name: str) -> None:
        self.name = name

    async def try_claim_and_run(self, queue: SharedQueue, *, name: str) -> WorkResult | None:
        item = await queue.claim_next(name)
        if item is None:
            return None
        return WorkResult(item_id=item.item_id, outcome="success")


@pytest.mark.asyncio
async def test_a_raising_worker_is_isolated_and_queue_still_drains():
    bad = _AlwaysRaisesWorker("bad")
    good = _EagerWorker("good")
    spec = WorkQueueSpec(
        workers={"bad": bad, "good": good},
        items=[WorkItem(item_id="i-0", payload=0), WorkItem(item_id="i-1", payload=1)],
        termination=TerminationPolicy(hard_cap=MaxRounds(max_rounds=20)),
    )

    completed: list[ItemCompletedEvent] = []
    drained: QueueDrainedEvent | None = None
    # Consume the whole stream — if the raising worker were fatal this would re-raise.
    async for event in work_queue_stream(spec):
        if isinstance(event, ItemCompletedEvent):
            completed.append(event)
        elif isinstance(event, QueueDrainedEvent):
            drained = event

    assert bad.raise_count >= 1  # the bad worker did run (and raised) without killing anything
    assert {e.item_id for e in completed} == {"i-0", "i-1"}
    assert drained is not None
    assert drained.reason.reason == "drained"


class _RaisesOnceThenSucceeds:
    """Raises on its first attempt (after claiming), then completes on retry.
    With an already-expired lease the half-claimed item is recovered and the
    worker succeeds the second time — proving a raise is recoverable, not a
    permanent death sentence for the item."""

    def __init__(self, name: str) -> None:
        self.name = name
        self._raised = False

    async def try_claim_and_run(self, queue: SharedQueue, *, name: str) -> WorkResult | None:
        item = await queue.claim_next(name)
        if item is None:
            return None
        if not self._raised:
            self._raised = True
            raise RuntimeError("transient blow-up after claim")
        return WorkResult(item_id=item.item_id, outcome="success")


@pytest.mark.asyncio
async def test_worker_that_raises_after_claim_recovers_via_lease_retry():
    worker = _RaisesOnceThenSucceeds("flaky")
    spec = WorkQueueSpec(
        workers={"flaky": worker},
        items=[WorkItem(item_id="only", payload="x")],
        lease_seconds=-1.0,  # already-expired — deterministic lease recovery, no sleeps
        termination=TerminationPolicy(hard_cap=MaxRounds(max_rounds=20)),
    )

    completed: list[ItemCompletedEvent] = []
    drained: QueueDrainedEvent | None = None
    async for event in work_queue_stream(spec):
        if isinstance(event, ItemCompletedEvent):
            completed.append(event)
        elif isinstance(event, QueueDrainedEvent):
            drained = event

    assert len(completed) == 1
    assert completed[0].item_id == "only"
    assert drained is not None
    assert drained.reason.reason == "drained"
