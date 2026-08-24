"""WorkQueue — pull-model work-stealing, the fifth coordination primitive
alongside :class:`~prodagent.coordination.spawn.Spawn` (``agents=``),
:class:`~prodagent.coordination.peer.Peer` (``peers=``),
:class:`~prodagent.coordination.ensemble.Ensemble` (``ensemble=``),
and :class:`~prodagent.coordination.blackboard.Blackboard`.

Where the other primitives *push* work at members (a spawn plan, a peer chain,
a speaking order, a trigger), a work queue is *pulled*: idle workers race to
claim the next pending item, run it, report success/failure. A claimed item is
leased for ``lease_seconds`` — if the worker never reports back (crash, hang),
the lease expires and the pipeline treats it as a failure, recycling it
through the same retry/dead-letter path as an explicit failure. Retry
accounting is delegated to :class:`~prodagent.ports.dead_letter.DeadLetterStore`
(also used by ``agents=`` for contract-violating child results) — after
``max_retries`` failures the item is archived instead of requeued.
"""

from __future__ import annotations

import logging
import time
from collections import deque
from dataclasses import dataclass, field
from enum import StrEnum
from typing import TYPE_CHECKING, Any, Literal, Protocol, runtime_checkable

from prodagent.coordination._stage import StageDriver
from prodagent.coordination._store import RoundedLockableStore
from prodagent.coordination.activation import Activation
from prodagent.coordination.messaging.envelope import (
    Crossing,
    CrossingKind,
    Direction,
)
from prodagent.coordination.messaging.pipeline import admission_pipeline
from prodagent.coordination.termination import (
    MaxRounds,
    TerminationPolicy,
    TerminationReason,
)
from prodagent.core.text import bound_text
from prodagent.core.event_log import Event

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator

    from prodagent.coordination.budget_ledger import BudgetLedger
    from prodagent.coordination.messaging.contract import MessageContract
    from prodagent.hooks.registry import HookRegistry
    from prodagent.ports.dead_letter import DeadLetterStore
    from prodagent.ports.event_log import EventLog

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


class QueueEventType(StrEnum):
    """Durable record of every SharedQueue transition — 1:1 with the in-memory
    mutations and the ephemeral ``WorkQueueEvent`` stream. Appended to an
    :class:`~prodagent.ports.event_log.EventLog` keyed by the queue's ``run_id``,
    so a crashed queue can be rebuilt by :meth:`SharedQueue.restore`."""

    ITEM_ENQUEUED = "ItemEnqueued"
    ITEM_CLAIMED = "ItemClaimed"
    ITEM_COMPLETED = "ItemCompleted"
    ITEM_REQUEUED = "ItemRequeued"
    ITEM_DEAD_LETTERED = "ItemDeadLettered"


def apply_queue_event(state: dict[str, Any], event: Event) -> None:
    """Fold one queue :class:`Event` into a rebuild-state dict — the pure kernel
    behind :meth:`SharedQueue.restore`, mirroring the plan domain's
    ``apply_event`` in ``runtime/plan/event_log.py``. State shape: ``pending``
    (id→WorkItem, insertion-ordered so replay preserves FIFO), ``claimed``
    (id→_ClaimInfo), ``completed`` ([id]), ``dead_lettered`` ([id]),
    ``resolutions`` (int)."""
    item_id = event.data["item_id"]
    etype = event.event_type
    if etype == QueueEventType.ITEM_ENQUEUED:
        state["pending"][item_id] = WorkItem(item_id, event.data["payload"], 0)
    elif etype == QueueEventType.ITEM_CLAIMED:
        item = state["pending"].pop(item_id, None)
        if item is None:
            item = WorkItem(item_id, event.data["payload"], 0)
        state["claimed"][item_id] = _ClaimInfo(
            event.data["worker"], item, event.data["lease_expires_at"]
        )
    elif etype == QueueEventType.ITEM_COMPLETED:
        state["claimed"].pop(item_id, None)
        state["completed"].append(item_id)
        state["resolutions"] += 1
    elif etype == QueueEventType.ITEM_REQUEUED:
        info = state["claimed"].pop(item_id, None)
        item = info.item if info is not None else WorkItem(item_id, event.data["payload"], 0)
        item.attempts = event.data["attempts"]
        state["pending"][item_id] = item
        state["resolutions"] += 1
    elif etype == QueueEventType.ITEM_DEAD_LETTERED:
        state["claimed"].pop(item_id, None)
        state["dead_lettered"].append(item_id)
        state["resolutions"] += 1


class SharedQueue(RoundedLockableStore):
    """``pending`` deque + ``claimed`` lease registry. A
    :class:`RoundedLockableStore` — the lock and round counter come from the
    base, shared with :class:`~prodagent.coordination.blackboard.Board`."""

    def __init__(
        self,
        items: list[WorkItem],
        *,
        dead_letter: DeadLetterStore,
        lease_seconds: float,
        event_log: EventLog | None = None,
        run_id: str = "",
    ) -> None:
        super().__init__()
        self._pending: deque[WorkItem] = deque(items)
        self._claimed: dict[str, _ClaimInfo] = {}
        self._completed: list[str] = []
        # Local mirror of dead-lettered item ids — fail() is the only path that
        # dead-letters, so this count is authoritative without a store round-trip
        # per snapshot()/fingerprint() (the driver polls both every round).
        self._dead_lettered: list[str] = []
        self._dead_letter = dead_letter
        self._lease_seconds = lease_seconds
        self._resolution_count = 0
        # Durable projection (optional). When set, every mutation appends a
        # QueueEventType event under ``run_id``; the in-memory state stays the
        # live source of truth during the run, the log is what survives a crash.
        self._event_log = event_log
        self._run_id = run_id
        self._last_seq = 0

    async def claim_next(self, worker_name: str) -> WorkItem | None:
        async with self._lock:
            if not self._pending:
                return None
            item = self._pending.popleft()
            lease_expires_at = time.monotonic() + self._lease_seconds
            self._claimed[item.item_id] = _ClaimInfo(
                worker=worker_name,
                item=item,
                lease_expires_at=lease_expires_at,
            )
            await self._record(
                QueueEventType.ITEM_CLAIMED,
                item_id=item.item_id,
                worker=worker_name,
                lease_expires_at=lease_expires_at,
                payload=item.payload,
            )
            return item

    async def complete(self, item_id: str) -> None:
        async with self._lock:
            if self._claimed.pop(item_id, None) is None:
                raise KeyError(f"complete() on unclaimed item {item_id!r}")
            self._completed.append(item_id)
            self._resolution_count += 1
            await self._record(QueueEventType.ITEM_COMPLETED, item_id=item_id)

    async def fail(self, item_id: str, error: str) -> tuple[Literal["dead_letter", "retry"], int]:
        """Record a failure for a claimed item. Delegates retry-vs-archive to
        the :class:`DeadLetterStore` — ``"retry"`` puts the item back on
        ``pending``; ``"dead_letter"`` archives it and drops it from the queue
        for good. Returns the decision plus the item's cumulative attempt count."""
        async with self._lock:
            claim = self._claimed.pop(item_id, None)
            if claim is None:
                raise KeyError(f"fail() on unclaimed item {item_id!r}")
            item = claim.item
            item.attempts += 1
            outcome = await self._dead_letter.on_failure(item_id, {"payload": item.payload}, error)
            if outcome == "retry":
                self._pending.append(item)
            else:
                self._dead_lettered.append(item_id)
            self._resolution_count += 1
            if outcome == "dead_letter":
                await self._record(
                    QueueEventType.ITEM_DEAD_LETTERED,
                    item_id=item_id,
                    attempts=item.attempts,
                    payload=item.payload,
                    error=error,
                )
            else:
                await self._record(
                    QueueEventType.ITEM_REQUEUED,
                    item_id=item_id,
                    attempts=item.attempts,
                    payload=item.payload,
                )
            return outcome, item.attempts

    def is_drained(self) -> bool:
        return not self._pending and not self._claimed

    async def dead_letters(self) -> list[dict[str, Any]]:
        """Full dead-letter records from the store — an operator console read,
        not something the round loop should poll (snapshot() carries the count)."""
        return await self._dead_letter.dead_letters()

    def snapshot(self) -> dict[str, Any]:
        return {
            "pending": len(self._pending),
            "claimed": len(self._claimed),
            "completed": len(self._completed),
            "dead_lettered": len(self._dead_lettered),
            "round_count": self._round_count,
            "elapsed_s": time.monotonic() - self.started_at,
        }

    def _expired_claim_ids(self, now: float) -> list[str]:
        return [
            item_id for item_id, claim in self._claimed.items() if claim.lease_expires_at <= now
        ]

    def _claim_worker(self, item_id: str) -> str | None:
        claim = self._claimed.get(item_id)
        return claim.worker if claim is not None else None

    def fingerprint(self) -> tuple[int, int, int, int, int]:
        """Liveness fingerprint — detects whether a round moved any item between
        pending/claimed/completed/dead-lettered. Even if a worker claimed and
        then reported nothing (crashed mid-task), the item is no longer in
        ``pending`` so it still counts as progress. ``_resolution_count`` covers
        the case a bare count snapshot would miss: a failed item requeued lands
        right back in ``pending``, leaving every count unchanged even though a
        real resolution happened."""
        return (
            len(self._pending),
            len(self._claimed),
            len(self._completed),
            len(self._dead_lettered),
            self._resolution_count,
        )

    async def _record(self, event_type: QueueEventType, **data: Any) -> int:
        """Append a durable event under ``run_id`` (mirrors ``PlanEventLog._record``:
        the optimistic ``expected_seq`` tail-check serializes appends under this
        store's lock). No-op when no event log is attached. Returns the assigned
        seq and advances ``_last_seq``."""
        if self._event_log is None:
            return 0
        seq = await self._event_log.append(
            Event.make(event_type, self._run_id, version=0, **data),
            expected_seq=self._last_seq,
        )
        self._last_seq = seq
        return seq

    async def record_enqueued(self) -> None:
        """Append ``ITEM_ENQUEUED`` for every pending item — called once when a
        durable queue starts fresh, so the log records the initial workload."""
        for item in list(self._pending):
            await self._record(
                QueueEventType.ITEM_ENQUEUED, item_id=item.item_id, payload=item.payload
            )

    @classmethod
    async def restore(
        cls,
        event_log: EventLog,
        run_id: str,
        *,
        dead_letter: DeadLetterStore,
        lease_seconds: float,
    ) -> SharedQueue:
        """Rebuild a SharedQueue by folding its event log — the crash-recovery
        path. Items claimed at crash time are reconstructed as ``claimed`` with
        their original lease; the resumed run's lease sweep requeues expired
        ones exactly as it would within a single run.

        Note: the in-memory ``DeadLetterStore`` is not itself event-sourced, so
        items dead-lettered before the crash are correctly absent from
        pending/claimed/completed but don't repopulate ``dead_letters()`` — full
        dead-letter durability is a follow-on (event-source that store too). The
        local ``_dead_lettered`` count *is* rebuilt from the event log, so
        snapshot()/fingerprint() stay crash-accurate."""
        events = await event_log.get_events(run_id)
        state: dict[str, Any] = {
            "pending": {},
            "claimed": {},
            "completed": [],
            "dead_lettered": [],
            "resolutions": 0,
        }
        for event in events:
            apply_queue_event(state, event)
        queue = cls(
            [],
            dead_letter=dead_letter,
            lease_seconds=lease_seconds,
            event_log=event_log,
            run_id=run_id,
        )
        queue._pending = deque(state["pending"].values())
        queue._claimed = state["claimed"]
        queue._completed = state["completed"]
        queue._dead_lettered = state["dead_lettered"]
        queue._resolution_count = state["resolutions"]
        queue._last_seq = events[-1].seq if events else 0
        return queue


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
    tokens: int = 0


@runtime_checkable
class Worker(Protocol):
    """Pull model — inverse of
    :class:`~prodagent.coordination.floor.FloorMember.speak` (push).
    Returns ``None`` if nothing was available to claim this round."""

    async def try_claim_and_run(self, queue: SharedQueue, *, name: str) -> WorkResult | None: ...


# ---------------------------------------------------------------------------
# WorkQueueSpec
# ---------------------------------------------------------------------------


def _in_memory_dlq() -> DeadLetterStore:
    from prodagent.backends.factory import in_memory_dead_letter_queue

    return in_memory_dead_letter_queue()


@dataclass
class WorkQueueSpec:
    workers: dict[str, Worker]
    items: list[WorkItem]
    lease_seconds: float = 30.0
    dead_letter: DeadLetterStore = field(default_factory=lambda: _in_memory_dlq())

    termination: TerminationPolicy = field(
        default_factory=lambda: TerminationPolicy(hard_cap=MaxRounds(max_rounds=100))
    )
    budget: BudgetLedger | None = None
    run_id: str = ""
    """Stable id for the durable event log (the EventLog partition key). When
    ``event_log`` is set and ``run_id`` already has events, the queue resumes
    from them; otherwise it starts fresh and records the workload."""
    event_log: EventLog | None = None
    """Optional durable projection — append every transition so the queue
    survives a crash and can be rebuilt via :meth:`SharedQueue.restore`."""

    payload_contract: MessageContract | None = None
    """Admission contract for item payloads, checked at construction — the
    whitelist-at-source rule. Fail-fast beats dead-lettering a workload that
    was born malformed: the durable event log then only ever records admitted
    payloads."""

    hooks: HookRegistry | None = None
    """Registry for the task-result gate. ``None`` (default) keeps the gate
    dormant — pass a registry once you register AGENT_HANDOFF checkers."""

    def __post_init__(self) -> None:
        if not self.workers:
            raise ValueError("WorkQueueSpec.workers cannot be empty")
        if not self.items:
            raise ValueError("WorkQueueSpec.items cannot be empty")
        ids = [item.item_id for item in self.items]
        if len(ids) != len(set(ids)):
            raise ValueError("WorkQueueSpec.items must have unique item_id values")
        if self.payload_contract is not None:
            for item in self.items:
                ok, error = self.payload_contract.validate(item.payload)
                if not ok:
                    raise ValueError(
                        f"WorkItem {item.item_id!r} payload rejected at enqueue: {error}"
                    )


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


class WorkQueue(StageDriver[WorkQueueEvent]):
    """Drives a work queue: each round, sweep expired leases back into the
    retry/dead-letter path, then let every worker race to claim-and-run once.

    Crash→error and finalize-to-unknown are handled by :class:`StageDriver`."""

    def __init__(self, spec: WorkQueueSpec) -> None:
        super().__init__()
        self._spec = spec
        self.queue = SharedQueue(
            spec.items,
            dead_letter=spec.dead_letter,
            lease_seconds=spec.lease_seconds,
            event_log=spec.event_log,
            run_id=spec.run_id,
        )
        self._budget = spec.budget
        self._opened = False
        # Task-result admission (UPSTREAM): a worker's report enters the
        # queue's resolution state through the messaging plane. Deliberately
        # no dead letter of its own — a governance rejection becomes an
        # ordinary failure that flows into the queue's existing fail() →
        # retry/dead-letter path, composing with rather than duplicating the
        # queue's error boundary.
        self._result_pipeline = admission_pipeline(trim=self._bound_error, hooks=spec.hooks)

    @staticmethod
    def _bound_error(payload: Any) -> Any:
        """Cap a worker's error text — one verbose crash must not flood every
        consumer of the queue's events."""
        if payload is not None and payload.error is not None and len(payload.error) > 2000:
            payload.error = bound_text(payload.error, 2000)
        return payload

    async def _open(self) -> None:
        """Lazy durable setup, run once before the first round. With an event
        log: resume from it when ``run_id`` already has events, else record the
        initial workload. No-op for non-durable queues."""
        if self._opened:
            return
        self._opened = True
        spec = self._spec
        if spec.event_log is None or not spec.run_id:
            return
        if await spec.event_log.get_events(spec.run_id):
            # Resume: rebuild the queue from its log, replacing the fresh one.
            self.queue = await SharedQueue.restore(
                spec.event_log,
                spec.run_id,
                dead_letter=spec.dead_letter,
                lease_seconds=spec.lease_seconds,
            )
        else:
            await self.queue.record_enqueued()

    async def _run_worker(self, worker_name: str) -> WorkResult | None:
        """Reserve → try_claim_and_run → commit for one worker, via the shared
        :meth:`StageDriver._run_enveloped`. A worker that can't reserve a turn
        never claims anything this round.

        A worker whose ``try_claim_and_run`` *raises* is treated as idle for this
        round, not allowed to kill the queue — mirroring how Ensemble/Blackboard
        isolate a failing member. The envelope releases its reservation (the
        crashed attempt doesn't consume a turn; the requeue retries and
        re-charges then). Any item it half-claimed stays in ``claimed`` and is
        recovered by the lease-expiry sweep, so a raise and a "crash after
        claim" (return ``None``) converge on the same lease-recovery path."""
        worker = self._spec.workers[worker_name]
        produced: list[WorkResult | None] = []

        async def _act() -> tuple[int, float] | None:
            result = await worker.try_claim_and_run(self.queue, name=worker_name)
            if result is None:
                produced.append(None)
                return None
            delivery = await self._result_pipeline.process(
                Crossing.mint(
                    direction=Direction.UPSTREAM,
                    kind=CrossingKind.TASK_RESULT,
                    from_agent=worker_name,
                    to=self._spec.run_id or "queue",
                    payload=result,
                )
            )
            if delivery.status == "rejected":
                result = WorkResult(
                    item_id=result.item_id,
                    outcome="failure",
                    error=f"rejected by admission: {delivery.reason}",
                    cost_usd=result.cost_usd,
                    tokens=result.tokens,
                )
            else:
                result = delivery.crossing.payload
            produced.append(result)
            return result.tokens, result.cost_usd

        try:
            await self._run_enveloped(worker_name, _act)
        except Exception as exc:  # noqa: BLE001 — isolate one bad worker from the queue
            logger.warning(
                "[work_queue] worker %s raised %s: %s — treating as idle this round; "
                "any held claim is lease-recovered",
                worker_name,
                type(exc).__name__,
                exc,
            )
            return None
        return produced[0] if produced else None

    async def _rounds(self) -> AsyncGenerator[WorkQueueEvent, None]:
        """One round per iteration: sweep expired leases, then fan out workers to
        claim-and-run once. Sets ``self._reason`` when the queue should stop.
        Crash→error and finalize-to-unknown are handled by :meth:`StageDriver.run`."""
        await self._open()
        round_num = 0
        while True:
            stop, policy_reason = self._spec.termination.should_stop(
                self.queue, next_round=round_num
            )
            if stop and policy_reason is not None:
                self._reason = policy_reason
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
                self._reason = TerminationReason(
                    reason="drained", detail="Queue fully drained — no pending or claimed items"
                )
                break

            before = self.queue.fingerprint()
            activation = Activation(
                members=list(self._spec.workers),
                dispatch="concurrent",
                round_num=round_num,
                label="pull",
            )
            results = await self._dispatch(activation, self._run_worker)

            for worker_name, result in results:
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

            any_progress = self.queue.fingerprint() != before
            if not any_progress and not self.queue.is_drained():
                self._reason = TerminationReason(
                    reason="no_progress",
                    detail="No worker claimed or completed anything this round",
                )
                break

            round_num += 1

    def _completed(self, reason: TerminationReason) -> WorkQueueEvent:
        return QueueDrainedEvent(reason=reason, queue_snapshot=self.queue.snapshot())


async def work_queue_stream(spec: WorkQueueSpec) -> AsyncGenerator[WorkQueueEvent, None]:
    """Drive a work queue and stream its events — parallel to ``blackboard_stream``."""
    pipeline = WorkQueue(spec)
    async for event in pipeline.run():
        yield event
