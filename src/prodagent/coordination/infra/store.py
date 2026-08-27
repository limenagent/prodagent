"""SharedStore — the one substrate behind the three stage coordination stores.

``Ensemble`` / ``Blackboard`` / ``WorkQueue`` each drive a shared mutable store:
``SharedFloor`` (append-only transcript), ``Board`` (versioned map), ``SharedQueue``
(lease-based claim deque). They are informal instances of one model — a store + an
activation policy (the *activation* axis is named explicitly in
:mod:`prodagent.ports.activation`) — and they share the same
*read-side contract*: a round-aware,
snapshotable state with a liveness fingerprint. That contract lives here, once,
so :class:`~prodagent.coordination.termination.TerminationPolicy` can
treat any store uniformly via ``round_count()`` and any driver can ask "did this
round make progress?" via ``fingerprint()``.

Two levels, because the stores are not identical:

- :class:`SharedStore` — the narrow contract all three satisfy (``round_count`` /
  ``snapshot`` / ``fingerprint`` / ``elapsed_seconds``). ``SharedFloor`` uses it
  directly: append-only with a single writer, so it has no lock and derives
  ``round_count`` from its turns.
- :class:`RoundedLockableStore` — ``Board`` and ``SharedQueue`` additionally
  share a lock + a stored round counter + ``_advance_round``; they sit on this
  sub-base (they fan out concurrent experts/workers per round).

This module is also the seam for Phase 2: event-sourcing the stores onto
:data:`~prodagent.ports.event_log.EventLog` so a coordination run survives a
crash and resumes — the durability the single-agent PLAN_FIRST executor already
has via ``runtime/plan/event_log.py``, extended to multi-agent coordination.
"""

from __future__ import annotations

import asyncio
import time
from abc import ABC, abstractmethod
from typing import Any

from prodagent.base.event_log import Event, append_expected

__all__ = ["SharedStore", "RoundedLockableStore", "EventSourcedStore"]


class EventSourcedStore:
    """Durable-projection mixin shared by the three stage stores: append one
    transition to the attached event log via the shared optimistic tail-check
    (``base.event_log.append_expected``), advancing the resume cursor.

    Lock-free by contract — the caller holds the store's lock (the queue and
    board mutate under theirs; the floor wraps its append in its own). The
    event type, the reducer, and ``restore`` stay in each domain: they are real
    content. What lives here once is the record-and-advance mechanics."""

    _event_log: Any
    _run_id: str
    _last_seq: int

    async def _record(self, event_type: Any, **data: Any) -> int:
        """Append one durable transition; no-op without an attached log.
        Returns the assigned seq and advances ``_last_seq``."""
        if getattr(self, "_event_log", None) is None or not getattr(self, "_run_id", ""):
            return 0
        seq = await append_expected(
            self._event_log,
            Event.make(event_type, self._run_id, version=0, **data),
            tail_seq=self._last_seq,
        )
        self._last_seq = seq
        return seq


class SharedStore(ABC):
    """Read-side contract every stage coordination store satisfies.

    Subclasses own the state and the write semantics (append / versioned-overwrite
    / claim-and-lease); this base fixes only what the driver and termination
    policy depend on. ``fingerprint`` is the liveness primitive: the driver
    compares it before/after a round to decide whether the round made progress.
    """

    started_at: float
    """Monotonic construction time — basis for :meth:`elapsed_seconds`. Set by
    each concrete store (a dataclass field on ``SharedFloor``; in ``__init__``
    on the rounded/lockable stores)."""

    def elapsed_seconds(self) -> float:
        """Wall-clock since this store was created — the budget 'seconds' axis."""
        return time.monotonic() - self.started_at

    @abstractmethod
    def round_count(self) -> int:
        """Current round index. Consumed by ``TerminationPolicy`` via the
        ``RoundCountable`` protocol."""
        ...

    @abstractmethod
    def snapshot(self) -> dict[str, Any]:
        """Serializable view of the store — for hooks / event log / playground."""
        ...

    @abstractmethod
    def fingerprint(self) -> Any:
        """Cheap fingerprint of the store's mutable state.

        The driver captures it before a round's work and compares after; an
        unchanged fingerprint means the round made no progress (``no_progress`` /
        ``no_contribution``). Must change whenever a mutating op succeeds and stay
        stable when nothing moved. Also the natural seam for event-sourced
        durability (Phase 2): the last applied event seq is a fingerprint."""
        ...


class RoundedLockableStore(SharedStore):
    """:class:`SharedStore` + one ``asyncio.Lock`` + a stored round counter.

    ``Board`` and ``SharedQueue`` fan out concurrent writers per round (experts /
    workers), so they serialize mutations under a single lock and track the round
    explicitly via ``_advance_round``. ``SharedFloor`` is append-only with a single
    writer and derives its round from turns, so it inherits :class:`SharedStore`
    directly instead of this class — hence the 2+1 split rather than one god-base.
    """

    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        self._round_count = 0
        self.started_at = time.monotonic()

    def round_count(self) -> int:
        return self._round_count

    def _advance_round(self, round_num: int) -> None:
        """Set the round index. Called by the driver at the top of each round so
        ``TerminationPolicy`` sees the round the store is about to enter."""
        self._round_count = round_num
