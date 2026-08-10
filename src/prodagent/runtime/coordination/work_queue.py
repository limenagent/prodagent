"""Work queue — pull-model work-stealing, the fifth coordination primitive
alongside :class:`~prodagent.runtime.coordination.spawn.Spawn` (``agents=``),
:class:`~prodagent.runtime.coordination.peer.Peer` (``peers=``),
:class:`~prodagent.runtime.coordination.ensemble.Ensemble` (``ensemble=``),
and :class:`~prodagent.runtime.coordination.blackboard.Blackboard`.

Where the other primitives *push* work at members (a spawn plan, a peer chain,
a speaking order, a trigger), a work queue is *pulled*: idle workers race to
claim the next pending item, run it, and report success/failure. A claimed
item is leased for ``lease_seconds`` — if the worker never reports back (crash,
hang), the item's lease expires and the pipeline treats it as a failure,
recycling it through the same retry/dead-letter path as an explicit failure.

Retry accounting is delegated to :class:`~prodagent.ports.dead_letter.DeadLetterStore`
(already used by ``agents=`` for contract-violating child results) rather than
reinvented here — after ``max_retries`` failures for the same item, it is
archived instead of requeued.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections import deque
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Literal, Protocol, runtime_checkable

from prodagent.backends.memory.dead_letter import InMemoryDeadLetterQueue
from prodagent.core.exceptions import BudgetExceeded
from prodagent.runtime.coordination.budget_ledger import BudgetLedger
from prodagent.runtime.coordination.termination import (
    MaxRounds,
    TerminationPolicy,
    TerminationReason,
)

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator

    from prodagent.ports.dead_letter import DeadLetterStore

logger = logging.getLogger(__name__)

__all__ = [
    "WorkItem",
    "SharedQueue",
    "Worker",
    "WorkResult",
    "WorkQueueSpec",
    "ItemClaimedEvent",
    "ItemCompletedEvent",
    "ItemRequeuedEvent",
    "ItemDeadLetteredEvent",
    "QueueDrainedEvent",
    "WorkQueue",
    "work_queue_stream",
]


# ---------------------------------------------------------------------------
# SharedQueue — pending/claimed/completed state, lease-based claims
# ---------------------------------------------------------------------------


@dataclass
class WorkItem:
    item_id: str
    payload: Any
    attempts: int = 0


@dataclass
class _ClaimInfo:
    worker: str
    item: WorkItem
    lease_expires_at: float


class SharedQueue:
    """``pending`` deque + ``claimed`` lease registry, guarded by one
    ``asyncio.Lock`` — the lock+mutable-state recipe shared with
    :class:`~prodagent.runtime.coordination.blackboard.Board` and
    :class:`BudgetLedger`."""

    def __init__(
        self,
        items: list[WorkItem],
        *,
        dead_letter: DeadLetterStore,
        lease_seconds: float,
    ) -> None:
        self._pending: deque[WorkItem] = deque(items)
        self._claimed: dict[str, _ClaimInfo] = {}
        self._completed: list[str] = []
        self._dead_letter = dead_letter
        self._lease_seconds = lease_seconds
        self._lock = asyncio.Lock()
        self._round_count = 0
        self._resolution_count = 0
        self.started_at = time.monotonic()

    async def claim_next(self, worker_name: str) -> WorkItem | None:
        async with self._lock:
            if not self._pending:
                return None
            item = self._pending.popleft()
            self._claimed[item.item_id] = _ClaimInfo(
                worker=worker_name,
                item=item,
                lease_expires_at=time.monotonic() + self._lease_seconds,
            )
            return item

    async def complete(self, item_id: str) -> None:
        async with self._lock:
            if self._claimed.pop(item_id, None) is None:
                raise KeyError(f"complete() on unclaimed item {item_id!r}")
            self._completed.append(item_id)
            self._resolution_count += 1

    async def fail(self, item_id: str, error: str) -> tuple[Literal["dead_letter", "retry"], int]:
        """Record a failure for a claimed item. Delegates the retry-vs-archive
        decision to the :class:`DeadLetterStore` — a ``"retry"`` outcome puts
        the item back on ``pending``; ``"dead_letter"`` archives it and drops
        it from the queue for good. Returns the decision plus the item's
        cumulative attempt count."""
        async with self._lock:
            claim = self._claimed.pop(item_id, None)
            if claim is None:
                raise KeyError(f"fail() on unclaimed item {item_id!r}")
            item = claim.item
            item.attempts += 1
            outcome = self._dead_letter.on_failure(item_id, {"payload": item.payload}, error)
            if outcome == "retry":
                self._pending.append(item)
            self._resolution_count += 1
            return outcome, item.attempts

    def is_drained(self) -> bool:
        return not self._pending and not self._claimed

    def dead_letters(self) -> list[dict[str, Any]]:
        return self._dead_letter.dead_letters()

    def snapshot(self) -> dict[str, Any]:
        return {
            "pending": len(self._pending),
            "claimed": len(self._claimed),
            "completed": len(self._completed),
            "dead_lettered": len(self._dead_letter.dead_letters()),
            "round_count": self._round_count,
            "elapsed_s": time.monotonic() - self.started_at,
        }

    def round_count(self) -> int:
        """Duck-typed for :class:`~prodagent.runtime.coordination.termination.TerminationPolicy`."""
        return self._round_count

    def _advance_round(self, round_num: int) -> None:
        self._round_count = round_num

    def _expired_claim_ids(self, now: float) -> list[str]:
        return [
            item_id
            for item_id, claim in self._claimed.items()
            if claim.lease_expires_at <= now
        ]

    def _claim_worker(self, item_id: str) -> str | None:
        claim = self._claimed.get(item_id)
        return claim.worker if claim is not None else None

    def _progress_marker(self) -> tuple[int, int, int, int, int]:
        """A cheap fingerprint of queue state, used to detect whether a round
        moved any item between pending/claimed/completed/dead-lettered — even
        if a worker claimed an item and then reported nothing (crashed
        mid-task), which still counts as progress since the item is no
        longer sitting in ``pending``. ``_resolution_count`` covers the case
        a bare pending/claimed/completed/dead-lettered *count* snapshot would
        miss: a failed item that gets requeued lands right back in
        ``pending``, leaving every count unchanged even though a real
        complete/fail resolution happened."""
        return (
            len(self._pending),
            len(self._claimed),
            len(self._completed),
            len(self._dead_letter.dead_letters()),
            self._resolution_count,
        )


# ---------------------------------------------------------------------------
# Worker protocol
# ---------------------------------------------------------------------------


@dataclass
class WorkResult:
    """A worker's report for one claim-and-run attempt."""

    item_id: str
    outcome: Literal["success", "failure"]
    error: str | None = None
    cost_usd: float = 0.0


@runtime_checkable
class Worker(Protocol):
    """Pull model — the inverse of
    :class:`~prodagent.runtime.coordination.floor.FloorMember.speak` (push).
    Returns ``None`` if nothing was available to claim this round."""

    async def try_claim_and_run(self, queue: SharedQueue, *, name: str) -> WorkResult | None: ...


# ---------------------------------------------------------------------------
# WorkQueueSpec
# ---------------------------------------------------------------------------


@dataclass
class WorkQueueSpec:
    workers: dict[str, Worker]
    items: list[WorkItem]
    lease_seconds: float = 30.0
    dead_letter: DeadLetterStore = field(default_factory=InMemoryDeadLetterQueue)
    termination: TerminationPolicy = field(
        default_factory=lambda: TerminationPolicy(hard_cap=MaxRounds(max_rounds=100))
    )
    budget: BudgetLedger | None = None

    def __post_init__(self) -> None:
        if not self.workers:
            raise ValueError("WorkQueueSpec.workers cannot be empty")
        if not self.items:
            raise ValueError("WorkQueueSpec.items cannot be empty")
        ids = [item.item_id for item in self.items]
        if len(ids) != len(set(ids)):
            raise ValueError("WorkQueueSpec.items must have unique item_id values")


# ---------------------------------------------------------------------------
# Events
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ItemClaimedEvent:
    item_id: str
    worker: str
    queue_snapshot: dict[str, Any]


@dataclass(frozen=True, slots=True)
class ItemCompletedEvent:
    item_id: str
    worker: str
    queue_snapshot: dict[str, Any]


@dataclass(frozen=True, slots=True)
class ItemRequeuedEvent:
    item_id: str
    worker: str | None
    reason: str
    queue_snapshot: dict[str, Any]


@dataclass(frozen=True, slots=True)
class ItemDeadLetteredEvent:
    item_id: str
    worker: str | None
    error: str
    attempts: int
    queue_snapshot: dict[str, Any]


@dataclass(frozen=True, slots=True)
class QueueDrainedEvent:
    reason: TerminationReason
    queue_snapshot: dict[str, Any]


# ---------------------------------------------------------------------------
# Pipeline
# ---------------------------------------------------------------------------

WorkQueueEvent = (
    ItemClaimedEvent
    | ItemCompletedEvent
    | ItemRequeuedEvent
    | ItemDeadLetteredEvent
    | QueueDrainedEvent
)


class WorkQueue:
    """Drives a work queue: each round, sweep expired leases back into the
    retry/dead-letter path, then let every worker race to claim-and-run once."""

    def __init__(self, spec: WorkQueueSpec) -> None:
        self._spec = spec
        self.queue = SharedQueue(
            spec.items, dead_letter=spec.dead_letter, lease_seconds=spec.lease_seconds
        )
        self._budget = spec.budget

    async def _run_worker(self, worker_name: str) -> WorkResult | None:
        """Reserve → try_claim_and_run → commit for one worker. A worker that
        can't reserve a turn never gets to claim anything this round."""
        if self._budget is not None:
            try:
                await self._budget.reserve(member=worker_name, turns=1)
            except BudgetExceeded:
                return None
        worker = self._spec.workers[worker_name]
        result = await worker.try_claim_and_run(self.queue, name=worker_name)
        if self._budget is not None:
            await self._budget.commit(
                member=worker_name,
                turns=1,
                tokens=0,
                cost_usd=result.cost_usd if result is not None else 0.0,
                reserved_turns=1,
            )
        return result

    async def run(self) -> AsyncGenerator[WorkQueueEvent, None]:
        reason: TerminationReason | None = None
        round_num = 0
        try:
            while True:
                stop, policy_reason = self._spec.termination.should_stop(
                    self.queue, next_round=round_num
                )
                if stop and policy_reason is not None:
                    reason = policy_reason
                    break

                self.queue._advance_round(round_num)

                now = time.monotonic()
                for item_id in self.queue._expired_claim_ids(now):
                    worker = self.queue._claim_worker(item_id)
                    outcome, attempts = await self.queue.fail(item_id, "lease expired")
                    if outcome == "dead_letter":
                        yield ItemDeadLetteredEvent(
                            item_id=item_id,
                            worker=worker,
                            error="lease expired",
                            attempts=attempts,
                            queue_snapshot=self.queue.snapshot(),
                        )
                    else:
                        yield ItemRequeuedEvent(
                            item_id=item_id,
                            worker=worker,
                            reason="lease expired",
                            queue_snapshot=self.queue.snapshot(),
                        )

                if self.queue.is_drained():
                    reason = TerminationReason(
                        reason="drained", detail="Queue fully drained — no pending or claimed items"
                    )
                    break

                before_marker = self.queue._progress_marker()
                results = await asyncio.gather(
                    *(self._run_worker(name) for name in self._spec.workers)
                )

                for worker_name, result in zip(self._spec.workers, results, strict=True):
                    if result is None:
                        continue
                    yield ItemClaimedEvent(
                        item_id=result.item_id,
                        worker=worker_name,
                        queue_snapshot=self.queue.snapshot(),
                    )
                    if result.outcome == "success":
                        await self.queue.complete(result.item_id)
                        yield ItemCompletedEvent(
                            item_id=result.item_id,
                            worker=worker_name,
                            queue_snapshot=self.queue.snapshot(),
                        )
                    else:
                        outcome, attempts = await self.queue.fail(
                            result.item_id, result.error or "unknown error"
                        )
                        if outcome == "dead_letter":
                            yield ItemDeadLetteredEvent(
                                item_id=result.item_id,
                                worker=worker_name,
                                error=result.error or "unknown error",
                                attempts=attempts,
                                queue_snapshot=self.queue.snapshot(),
                            )
                        else:
                            yield ItemRequeuedEvent(
                                item_id=result.item_id,
                                worker=worker_name,
                                reason=result.error or "unknown error",
                                queue_snapshot=self.queue.snapshot(),
                            )

                any_progress = self.queue._progress_marker() != before_marker
                if not any_progress and not self.queue.is_drained():
                    reason = TerminationReason(
                        reason="no_progress",
                        detail="No worker claimed or completed anything this round",
                    )
                    break

                round_num += 1

            if reason is None:
                reason = TerminationReason(
                    reason="unknown", detail="Work queue exited loop without a termination reason"
                )
            yield QueueDrainedEvent(reason=reason, queue_snapshot=self.queue.snapshot())
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 — surface as a failed event, don't crash the stream
            logger.exception("[work_queue] pipeline crashed: %s", exc)
            yield QueueDrainedEvent(
                reason=TerminationReason(
                    reason="error", detail=f"{type(exc).__name__}: {exc}", by_hard_cap=False
                ),
                queue_snapshot=self.queue.snapshot(),
            )


async def work_queue_stream(spec: WorkQueueSpec) -> AsyncGenerator[WorkQueueEvent, None]:
    """Drive a work queue and stream its events — parallel to ``blackboard_stream``."""
    pipeline = WorkQueue(spec)
    async for event in pipeline.run():
        yield event
