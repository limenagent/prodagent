"""Work queue — dead letter: an item that fails past its retry budget is
archived instead of requeued forever, and the queue still drains."""

from __future__ import annotations

import pytest

from prodagent.backends.memory.dead_letter import InMemoryDeadLetterQueue
from prodagent.runtime.coordination.termination import MaxRounds, TerminationPolicy
from prodagent.runtime.coordination.work_queue import (
    ItemCompletedEvent,
    ItemDeadLetteredEvent,
    QueueDrainedEvent,
    SharedQueue,
    WorkItem,
    WorkQueueSpec,
    WorkResult,
    work_queue_stream,
)


class _AlwaysFailsWorker:
    def __init__(self, name: str) -> None:
        self.name = name

    async def try_claim_and_run(self, queue: SharedQueue, *, name: str) -> WorkResult | None:
        item = await queue.claim_next(name)
        if item is None:
            return None
        return WorkResult(item_id=item.item_id, outcome="failure", error="boom")


@pytest.mark.asyncio
async def test_item_exhausting_retries_is_dead_lettered_not_requeued_forever():
    spec = WorkQueueSpec(
        workers={"cursed": _AlwaysFailsWorker("cursed")},
        items=[WorkItem(item_id="poison-pill", payload="x")],
        dead_letter=InMemoryDeadLetterQueue(max_retries=3),
        termination=TerminationPolicy(hard_cap=MaxRounds(max_rounds=20)),
    )

    dead_lettered: list[ItemDeadLetteredEvent] = []
    drained: QueueDrainedEvent | None = None
    async for event in work_queue_stream(spec):
        if isinstance(event, ItemDeadLetteredEvent):
            dead_lettered.append(event)
        elif isinstance(event, QueueDrainedEvent):
            drained = event

    assert len(dead_lettered) == 1
    assert dead_lettered[0].item_id == "poison-pill"
    assert dead_lettered[0].error == "boom"
    assert dead_lettered[0].attempts == 3
    assert drained is not None
    assert drained.reason.reason == "drained"
    assert drained.queue_snapshot["dead_lettered"] == 1
    assert drained.queue_snapshot["pending"] == 0
    assert drained.queue_snapshot["claimed"] == 0


class _FailsOnceThenSucceedsWorker:
    def __init__(self, name: str) -> None:
        self.name = name
        self._seen: set[str] = set()

    async def try_claim_and_run(self, queue: SharedQueue, *, name: str) -> WorkResult | None:
        item = await queue.claim_next(name)
        if item is None:
            return None
        if item.item_id not in self._seen:
            self._seen.add(item.item_id)
            return WorkResult(item_id=item.item_id, outcome="failure", error="transient")
        return WorkResult(item_id=item.item_id, outcome="success")


@pytest.mark.asyncio
async def test_item_recovers_on_retry_before_hitting_dead_letter_ceiling():
    spec = WorkQueueSpec(
        workers={"flaky": _FailsOnceThenSucceedsWorker("flaky")},
        items=[WorkItem(item_id="recoverable", payload="x")],
        dead_letter=InMemoryDeadLetterQueue(max_retries=3),
        termination=TerminationPolicy(hard_cap=MaxRounds(max_rounds=20)),
    )

    completed: list[ItemCompletedEvent] = []
    dead_lettered: list[ItemDeadLetteredEvent] = []
    async for event in work_queue_stream(spec):
        if isinstance(event, ItemCompletedEvent):
            completed.append(event)
        elif isinstance(event, ItemDeadLetteredEvent):
            dead_lettered.append(event)

    assert dead_lettered == []
    assert len(completed) == 1
    assert completed[0].item_id == "recoverable"
