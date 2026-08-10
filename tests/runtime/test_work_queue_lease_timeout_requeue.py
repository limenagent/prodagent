"""Work queue — lease timeout: a worker that claims an item and never reports
back (crash, hang) leaks its lease; the pipeline's per-round sweep notices
the expired lease and requeues the item for another worker to pick up."""

from __future__ import annotations

import pytest

from prodagent.runtime.coordination.termination import MaxRounds, TerminationPolicy
from prodagent.runtime.coordination.work_queue import (
    ItemCompletedEvent,
    ItemRequeuedEvent,
    QueueDrainedEvent,
    SharedQueue,
    WorkItem,
    WorkQueueSpec,
    WorkResult,
    work_queue_stream,
)


class _CrashesOnFirstClaimWorker:
    """Claims an item and then "hangs" — never returns a WorkResult for it,
    simulating a worker process that died mid-task. Only crashes once; any
    item it claims after that it completes normally (so a requeued item
    picked up by this same worker on a later round succeeds)."""

    def __init__(self, name: str) -> None:
        self.name = name
        self.has_crashed_once = False

    async def try_claim_and_run(self, queue: SharedQueue, *, name: str) -> WorkResult | None:
        item = await queue.claim_next(name)
        if item is None:
            return None
        if not self.has_crashed_once:
            self.has_crashed_once = True
            return None  # "crash" — item stays claimed, lease will expire
        return WorkResult(item_id=item.item_id, outcome="success")


@pytest.mark.asyncio
async def test_expired_lease_requeues_item_for_another_worker():
    worker = _CrashesOnFirstClaimWorker("flaky")
    spec = WorkQueueSpec(
        workers={"flaky": worker},
        items=[WorkItem(item_id="only-item", payload="x")],
        lease_seconds=-1.0,  # already-expired lease — deterministic, no sleeps
        termination=TerminationPolicy(hard_cap=MaxRounds(max_rounds=10)),
    )

    requeued: list[ItemRequeuedEvent] = []
    completed: list[ItemCompletedEvent] = []
    drained: QueueDrainedEvent | None = None
    async for event in work_queue_stream(spec):
        if isinstance(event, ItemRequeuedEvent):
            requeued.append(event)
        elif isinstance(event, ItemCompletedEvent):
            completed.append(event)
        elif isinstance(event, QueueDrainedEvent):
            drained = event

    assert len(requeued) == 1
    assert requeued[0].item_id == "only-item"
    assert requeued[0].reason == "lease expired"
    assert len(completed) == 1
    assert completed[0].item_id == "only-item"
    assert drained is not None
    assert drained.reason.reason == "drained"
