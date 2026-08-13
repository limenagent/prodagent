"""WorkQueue durability slice — a SharedQueue projected onto an EventLog can be
rebuilt by ``SharedQueue.restore`` (replay fidelity) and survives a crash
mid-claim (lease recovery across a restart). Uses the real ``FileEventLog``
backend — no in-memory stub — so persistence is exercised end to end."""

from __future__ import annotations

import pytest

from prodagent.backends.file.event_log import FileEventLog
from prodagent.backends.memory.dead_letter import InMemoryDeadLetterQueue
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


@pytest.mark.asyncio
async def test_restore_replays_pending_claimed_completed(tmp_path):
    log = FileEventLog(tmp_path)
    q = SharedQueue(
        [WorkItem("a", "pa"), WorkItem("b", "pb")],
        dead_letter=InMemoryDeadLetterQueue(),
        lease_seconds=30.0,
        event_log=log,
        run_id="run-1",
    )
    await q.record_enqueued()

    item_a = await q.claim_next("w")
    assert item_a is not None and item_a.item_id == "a"
    await q.complete("a")
    item_b = await q.claim_next("w")
    assert item_b is not None and item_b.item_id == "b"
    await q.fail("b", "boom")  # retry → b requeued

    restored = await SharedQueue.restore(
        log, "run-1", dead_letter=InMemoryDeadLetterQueue(), lease_seconds=30.0
    )
    assert [i.item_id for i in restored._pending] == ["b"]
    assert restored._completed == ["a"]
    assert restored._claimed == {}
    assert restored._last_seq == q._last_seq


@pytest.mark.asyncio
async def test_crash_mid_claim_is_lease_recovered_on_resume(tmp_path):
    log = FileEventLog(tmp_path)
    # Worker claims the only item then "crashes" — ITEM_CLAIMED is logged, the
    # item is left claimed, the run is discarded.
    q = SharedQueue(
        [WorkItem("only", "x")],
        dead_letter=InMemoryDeadLetterQueue(),
        lease_seconds=-1.0,  # already-expired lease — deterministic recovery, no sleeps
        event_log=log,
        run_id="run-2",
    )
    await q.record_enqueued()
    claimed = await q.claim_next("w1")
    assert claimed is not None and claimed.item_id == "only"
    del q  # simulate the process dying mid-task

    class _Eager:
        def __init__(self, name: str) -> None:
            self.name = name

        async def try_claim_and_run(self, queue: SharedQueue, *, name: str) -> WorkResult | None:
            item = await queue.claim_next(name)
            if item is None:
                return None
            return WorkResult(item_id=item.item_id, outcome="success")

    # Resume on run-2: the WorkQueue restores from the log, the expired claim is
    # swept + requeued, and the worker completes the item.
    spec = WorkQueueSpec(
        workers={"w": _Eager("w")},
        items=[WorkItem("only", "x")],
        lease_seconds=-1.0,
        termination=TerminationPolicy(hard_cap=MaxRounds(max_rounds=20)),
        run_id="run-2",
        event_log=log,
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
