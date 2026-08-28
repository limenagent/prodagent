"""Work queue — claim: concurrent workers race to claim disjoint items off a
shared pending deque, and each item is claimed by exactly one worker."""

from __future__ import annotations

import pytest

from prodagent.coordination.infra.stage import MaxRounds, TerminationPolicy
from prodagent.coordination.work_queue import (
    ItemClaimedEvent,
    ItemCompletedEvent,
    QueueDrainedEvent,
    SharedQueue,
    WorkItem,
    WorkQueueSpec,
    WorkResult,
    work_queue_stream,
)


class _EagerWorker:
    """Claims and completes exactly one item per round, if any is available."""

    def __init__(self, name: str) -> None:
        self.name = name
        self.completed: list[str] = []

    async def try_claim_and_run(self, queue: SharedQueue, *, name: str) -> WorkResult | None:
        item = await queue.claim_next(name)
        if item is None:
            return None
        self.completed.append(item.item_id)
        return WorkResult(item_id=item.item_id, outcome="success")


@pytest.mark.asyncio
async def test_two_workers_drain_five_items_with_no_double_claims():
    workers = {"w1": _EagerWorker("w1"), "w2": _EagerWorker("w2")}
    items = [WorkItem(item_id=f"item-{i}", payload=i) for i in range(5)]
    spec = WorkQueueSpec(
        workers=workers,
        items=items,
        termination=TerminationPolicy(hard_cap=MaxRounds(max_rounds=20)),
    )

    claimed_events: list[ItemClaimedEvent] = []
    completed_events: list[ItemCompletedEvent] = []
    drained: QueueDrainedEvent | None = None
    async for event in work_queue_stream(spec):
        if isinstance(event, ItemClaimedEvent):
            claimed_events.append(event)
        elif isinstance(event, ItemCompletedEvent):
            completed_events.append(event)
        elif isinstance(event, QueueDrainedEvent):
            drained = event

    claimed_ids = [e.item_id for e in claimed_events]
    assert sorted(claimed_ids) == sorted(i.item_id for i in items)
    assert len(claimed_ids) == len(set(claimed_ids))  # no item claimed twice
    assert {e.item_id for e in completed_events} == {i.item_id for i in items}
    assert drained is not None
    assert drained.reason.reason == "drained"
    assert drained.queue_snapshot["completed"] == 5
    assert drained.queue_snapshot["pending"] == 0
    assert drained.queue_snapshot["claimed"] == 0

    # Every item went to exactly one worker's private completed list.
    all_completed = workers["w1"].completed + workers["w2"].completed
    assert sorted(all_completed) == sorted(i.item_id for i in items)


class _IdleWorker:
    """Never claims anything — used to prove an all-idle queue reports
    no_progress instead of spinning forever."""

    def __init__(self, name: str) -> None:
        self.name = name

    async def try_claim_and_run(self, queue: SharedQueue, *, name: str) -> WorkResult | None:
        return None


@pytest.mark.asyncio
async def test_no_worker_claiming_anything_reports_no_progress():
    spec = WorkQueueSpec(
        workers={"idle": _IdleWorker("idle")},
        items=[WorkItem(item_id="stuck", payload=None)],
    )

    drained: QueueDrainedEvent | None = None
    async for event in work_queue_stream(spec):
        if isinstance(event, QueueDrainedEvent):
            drained = event

    assert drained is not None
    assert drained.reason.reason == "no_progress"
    assert drained.queue_snapshot["pending"] == 1


@pytest.mark.asyncio
async def test_retry_backoff_hides_item_until_visible():
    """A retried item with backoff is unclaimable until its earliest-visible
    time; later items stay claimable (skip, not reorder)."""
    from prodagent.backends.memory.dead_letter import InMemoryDeadLetterQueue

    queue = SharedQueue(
        [WorkItem(item_id="a", payload=1), WorkItem(item_id="b", payload=2)],
        dead_letter=InMemoryDeadLetterQueue(max_retries=3),
        lease_seconds=30.0,
        retry_backoff_seconds=5.0,
    )
    first = await queue.claim_next("w1")
    assert first is not None and first.item_id == "a"
    outcome, attempts = await queue.fail(first.item_id, "boom")
    assert (outcome, attempts) == ("retry", 1)

    # Backoff hides "a"; the next claim skips to "b", order otherwise FIFO.
    second = await queue.claim_next("w2")
    assert second is not None and second.item_id == "b"

    # After the backoff window, "a" is claimable again.
    for item in queue._pending:
        item.not_before = 0.0
    again = await queue.claim_next("w3")
    assert again is not None and again.item_id == "a"


@pytest.mark.asyncio
async def test_zero_backoff_keeps_immediate_retry():
    """retry_backoff_seconds=0 (default) preserves the historical contract:
    a retried item is claimable again immediately."""
    from prodagent.backends.memory.dead_letter import InMemoryDeadLetterQueue

    queue = SharedQueue(
        [WorkItem(item_id="a", payload=1)],
        dead_letter=InMemoryDeadLetterQueue(max_retries=3),
        lease_seconds=30.0,
    )
    first = await queue.claim_next("w1")
    assert first is not None
    outcome, _ = await queue.fail(first.item_id, "boom")
    assert outcome == "retry"
    again = await queue.claim_next("w2")
    assert again is not None and again.item_id == "a" and again.attempts == 1
