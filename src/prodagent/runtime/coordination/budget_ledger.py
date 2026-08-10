"""BudgetLedger — the shared reserve/commit accumulator behind every coordination primitive.

Three coordination primitives (``agents=``, ``peers=``, ``ensemble=``) each grew
their own way of tracking cross-run spend: :class:`~prodagent.runtime.coordination.accounting.SpawnAccumulator`
(after-the-fact, unlocked, ``agents=``), nothing at all (``peers=``), and the
original ``SharedBudget`` in this module's former single-class form
(reserve/commit with a lock, ``ensemble=`` — the only one that was actually
safe under concurrency). ``BudgetLedger`` is the one implementation the other
two now build on, so a fix here (or a new primitive's budget needs) doesn't
mean inventing a fourth ledger.

Two real bugs in the original ``SharedBudget`` are fixed here:

1. **No way to give back a reservation.** A reservation that turns out not to
   have happened (a losing bid in a lock race, a task requeued before it ran)
   had no clean way to undo itself — callers were reduced to committing a zero
   actual against a nonzero reservation, which reads backwards. :meth:`release`
   is that undo, named for what it does.
2. **Permanent latch on transient overshoot.** The old ``_exhausted: bool``
   flag, once set by *any* over-cap moment (including a reservation that was
   later released or reconciled back under cap), stayed set forever — a
   single momentary overshoot could permanently stop a floor/chain/queue even
   though its real committed spend was fine. The fix: track *committed*
   (permanent, monotonically non-decreasing, touched only by real ``commit``
   deltas) and *reserved* (transient, touched by ``reserve``/``release`` and
   reconciled by ``commit``) separately. The over-cap test is
   ``committed + reserved >= cap``. Once outstanding reservations are
   released or reconciled, the sum can fall back under cap and further
   ``check()``/``reserve()`` calls succeed again — self-healing for transient
   overshoot, while a cap breached by ``committed`` alone (real, confirmed
   spend) can never un-latch, because ``committed`` never decreases. That is
   the correct "permanent stop" the old flag was reaching for, without the
   bug of also latching on reservations that never became real spend.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from prodagent.core.exceptions import BudgetExceeded

if TYPE_CHECKING:
    from prodagent.core.budget import HardBudget

logger = logging.getLogger(__name__)


__all__ = ["BudgetLedger", "BudgetAxis", "SharedBudget", "spent_to_dict"]


class BudgetAxis:
    """Axis labels — match :class:`BudgetExceeded` context values."""

    TURNS = "turns"
    SECONDS = "seconds"
    TOKENS = "tokens"
    COST_USD = "cost_usd"


@dataclass
class _Spend:
    """Mutable spend counters — protected by BudgetLedger's lock."""

    turns: int = 0
    tokens: int = 0
    cost_usd: float = 0.0


@dataclass
class BudgetLedger:
    """Four-axis ceiling shared across concurrent spenders by reference.

    Construct once per coordinated run (ensemble floor, peer chain, a batch of
    concurrent spawns), pass to every spender. Call :meth:`reserve` before a
    unit of work starts (optional — skip it for spenders that can't estimate
    ahead of time), :meth:`commit` after it finishes with the real cost, and
    :meth:`release` if a reservation never turned into real work (a losing
    lock-race bid, a requeued task).

    ``seconds`` is wall-clock since construction, always derived from
    ``time.monotonic()`` — it is never reserved/committed/released like the
    other three axes.
    """

    max: HardBudget
    """The ceiling. Same shape as a per-run HardBudget — turns/seconds/tokens/cost."""

    _committed: _Spend = field(default_factory=_Spend)
    """Permanent — only moved by commit()'s actual deltas. Never decreases."""

    _reserved: _Spend = field(default_factory=_Spend)
    """Transient — moved by reserve()/release(), reconciled away by commit()."""

    _lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    _start_monotonic: float = field(default_factory=time.monotonic)

    @property
    def spent(self) -> _Spend:
        """Read-only snapshot of current (committed + reserved) spend — best-effort, no lock."""
        return _Spend(
            turns=self._committed.turns + self._reserved.turns,
            tokens=self._committed.tokens + self._reserved.tokens,
            cost_usd=self._committed.cost_usd + self._reserved.cost_usd,
        )

    def elapsed_seconds(self) -> float:
        """Wall-clock since this BudgetLedger was created — the 'seconds' axis."""
        return time.monotonic() - self._start_monotonic

    def is_exhausted(self) -> bool:
        """Cheap pre-check without taking the lock — pollable between rounds/hops.

        Authoritative check (raises with axis detail) is :meth:`check`.
        """
        return self._is_over_cap_locked()

    async def check(self, *, member: str) -> None:
        """Raise :class:`BudgetExceeded` if committed+reserved is at or over cap.

        Call this before a unit of work starts. Does not debit.
        """
        async with self._lock:
            self._raise_if_over_cap_locked(member=member)

    async def reserve(
        self,
        *,
        member: str,
        turns: int = 1,
        tokens: int = 0,
        cost_usd: float = 0.0,
    ) -> None:
        """Pre-debit an estimate before a unit of work starts.

        Optional — used when a spender can estimate ahead of time and wants
        concurrent siblings to see this work as "already spoken for". Raises
        (without debiting) if already at/over cap. The debit is transient:
        reconciled away by :meth:`commit` or undone entirely by :meth:`release`.
        """
        async with self._lock:
            self._raise_if_over_cap_locked(member=member)
            self._reserved.turns += turns
            self._reserved.tokens += tokens
            self._reserved.cost_usd += cost_usd
            logger.debug(
                "[budget_ledger] reserved for %s: +turns=%d +tokens=%d +cost=%.4f → %s",
                member,
                turns,
                tokens,
                cost_usd,
                self._snapshot_locked(),
            )

    async def release(
        self,
        *,
        member: str,
        reserved_turns: int = 0,
        reserved_tokens: int = 0,
        reserved_cost_usd: float = 0.0,
    ) -> None:
        """Give back a reservation that never became real spend.

        Use when a reserved unit of work didn't happen — a losing lock-race
        bid, a task requeued before it ran. Unlike :meth:`commit`, this never
        touches ``_committed`` and can only ever reduce outstanding reserved
        spend, so it can only help a caller pass a subsequent :meth:`check`.
        """
        async with self._lock:
            self._reserved.turns = max(0, self._reserved.turns - reserved_turns)
            self._reserved.tokens = max(0, self._reserved.tokens - reserved_tokens)
            self._reserved.cost_usd = max(0.0, self._reserved.cost_usd - reserved_cost_usd)
            logger.debug(
                "[budget_ledger] released for %s: -turns=%d -tokens=%d -cost=%.4f → %s",
                member,
                reserved_turns,
                reserved_tokens,
                reserved_cost_usd,
                self._snapshot_locked(),
            )

    async def commit(
        self,
        *,
        member: str,
        turns: int,
        tokens: int,
        cost_usd: float,
        reserved_turns: int = 0,
        reserved_tokens: int = 0,
        reserved_cost_usd: float = 0.0,
    ) -> None:
        """Reconcile a reservation (if any) against the actual spend, and commit it.

        Removes the reservation (so it isn't double-counted) and adds the
        actual deltas to the permanent ``_committed`` ledger. If the result is
        at/over cap, further ``check``/``reserve`` calls will refuse — but
        unlike the old latch, this can self-heal: if the overshoot came from
        *other* outstanding reservations rather than this commit's own actual
        spend, a later :meth:`release` of those can bring the total back
        under cap.
        """
        async with self._lock:
            self._reserved.turns = max(0, self._reserved.turns - reserved_turns)
            self._reserved.tokens = max(0, self._reserved.tokens - reserved_tokens)
            self._reserved.cost_usd = max(0.0, self._reserved.cost_usd - reserved_cost_usd)
            self._committed.turns += turns
            self._committed.tokens += tokens
            self._committed.cost_usd += cost_usd
            spent_now = self._snapshot_locked()
            logger.debug(
                "[budget_ledger] committed for %s: turns=%d tokens=%d cost=%.4f → %s",
                member,
                turns,
                tokens,
                cost_usd,
                spent_now,
            )
            if self._is_over_cap_locked():
                logger.warning(
                    "[budget_ledger] over cap after %s's commit: %s",
                    member,
                    spent_now,
                )

    def _raise_if_over_cap_locked(self, *, member: str) -> None:
        if not self._is_over_cap_locked():
            return
        turns = self._committed.turns + self._reserved.turns
        tokens = self._committed.tokens + self._reserved.tokens
        cost_usd = self._committed.cost_usd + self._reserved.cost_usd
        elapsed = self.elapsed_seconds()
        # Precedence matches the original SharedBudget: turns, seconds, tokens, cost.
        axis: str
        value: float
        limit: float
        if turns >= self.max.max_turns:
            axis, value, limit = BudgetAxis.TURNS, turns, self.max.max_turns
        elif elapsed >= self.max.max_seconds:
            axis, value, limit = BudgetAxis.SECONDS, elapsed, self.max.max_seconds
        elif tokens >= self.max.max_tokens:
            axis, value, limit = BudgetAxis.TOKENS, tokens, self.max.max_tokens
        else:
            axis, value, limit = BudgetAxis.COST_USD, cost_usd, self.max.max_cost_usd
        raise BudgetExceeded(
            f"Shared budget ledger exhausted on {axis} axis: "
            f"{value}/{limit} (member {member!r} blocked)",
            axis=axis,
            value=value,
            limit=limit,
            member=member,
            floor_spent=spent_to_dict(self.spent, elapsed=elapsed),
        )

    def _is_over_cap_locked(self) -> bool:
        turns = self._committed.turns + self._reserved.turns
        tokens = self._committed.tokens + self._reserved.tokens
        cost_usd = self._committed.cost_usd + self._reserved.cost_usd
        return (
            turns >= self.max.max_turns
            or self.elapsed_seconds() >= self.max.max_seconds
            or tokens >= self.max.max_tokens
            or cost_usd >= self.max.max_cost_usd
        )

    def _snapshot_locked(self) -> dict[str, float | int]:
        return spent_to_dict(self.spent, elapsed=self.elapsed_seconds())


def spent_to_dict(spend: _Spend, *, elapsed: float) -> dict[str, float | int]:
    return {
        "turns": spend.turns,
        "seconds": round(elapsed, 2),
        "tokens": spend.tokens,
        "cost_usd": round(spend.cost_usd, 6),
    }


SharedBudget = BudgetLedger
