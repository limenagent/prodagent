"""SharedBudget — the cross-member hard ceiling that stops an ensemble run.

:class:`~prodagent.core.budget.HardBudget` is per-``AgentRun``: each member of
an ensemble has its own ``HardBudget`` and its own ``check_budget`` call.
Nothing in that machinery stops three members each burning their full quota —
the floor as a whole triples its spend. :class:`SharedBudget` is the missing
piece: one async-safe accumulator held by reference across all members,
summing turns / seconds / tokens / cost, checked before each member speaks.

Contrast with :class:`~prodagent.runtime.coordination.accounting.SpawnAccumulator`:
that folds *completed* child spend onto the parent run after the fact — book-
keeping, not real-time reservation. ``SharedBudget`` reserves before a turn
runs and commits the actual delta after — so a member that's about to blow
the cap is stopped before the LLM call, not merely noted afterwards.

The reservation is best-effort: we reserve the member's *own* per-turn budget
(its ``HardBudget`` scaled down), then commit the real cost. Under
concurrency this can over-reserve slightly; the hard cap on commit is the
backstop. This is deliberate — strictly preventing any over-spend would
require serializing all member LLM calls, which defeats the point of allowing
concurrent members in the ``FreeForAll`` order (deferred, but the budget must
not preclude it).
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


__all__ = ["SharedBudget", "BudgetAxis"]


class BudgetAxis:
    """Axis labels — match :class:`BudgetExceeded` context values."""

    TURNS = "turns"
    SECONDS = "seconds"
    TOKENS = "tokens"
    COST_USD = "cost_usd"


@dataclass
class _Spend:
    """Mutable spend ledger — protected by SharedBudget's lock."""

    turns: int = 0
    seconds: float = 0.0
    tokens: int = 0
    cost_usd: float = 0.0


@dataclass
class SharedBudget:
    """Four-axis ceiling shared across all ensemble members by reference.

    Construct once per ensemble run, pass to every member's turn. The pipeline
    calls :meth:`check` before scheduling a member and :meth:`commit` after the
    turn returns. ``reserve`` is optional — used by orders that fan out
    concurrently to pre-debit an estimate so a late-starter sees the spent
    total.
    """

    max: HardBudget
    """The ceiling. Same shape as a per-run HardBudget — turns/seconds/tokens/cost."""

    _spent: _Spend = field(default_factory=_Spend)
    _lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    _start_monotonic: float = field(default_factory=time.monotonic)
    _exhausted: bool = False
    """Latched — once True, no further reserves/commits succeed. Prevents a
    member that committed slightly over-cap from being followed by another
    that thinks there's still room."""

    @property
    def spent(self) -> _Spend:
        """Read-only snapshot of current spend (lock not held — best-effort)."""
        return _Spend(
            turns=self._spent.turns,
            seconds=self._spent.seconds,
            tokens=self._spent.tokens,
            cost_usd=self._spent.cost_usd,
        )

    def elapsed_seconds(self) -> float:
        """Wall-clock since this SharedBudget was created — the 'seconds' axis."""
        return time.monotonic() - self._start_monotonic

    def is_exhausted(self) -> bool:
        """Cheap pre-check without taking the lock — pipeline can poll this
        between rounds. Authoritative check is :meth:`check`."""
        if self._exhausted:
            return True
        return (
            self._spent.turns >= self.max.max_turns
            or self.elapsed_seconds() >= self.max.max_seconds
            or self._spent.tokens >= self.max.max_tokens
            or self._spent.cost_usd >= self.max.max_cost_usd
        )

    async def check(self, *, member: str) -> None:
        """Raise :class:`BudgetExceeded` if the floor is at or over cap.

        Call this *before* a member speaks. Does not debit — :meth:`commit`
        does that after the turn. Locking here is so a concurrent
        ``FreeForAll`` starter doesn't see a stale sub-cap snapshot right as
        another member is committing.
        """
        async with self._lock:
            self._raise_if_exhausted_locked(member=member)

    async def reserve(
        self,
        *,
        member: str,
        turns: int = 1,
        tokens: int = 0,
        cost_usd: float = 0.0,
    ) -> None:
        """Pre-debit an estimate before a turn runs.

        Optional. Used when a member is about to be scheduled and we want the
        next starter to see this turn's cost as 'already spoken for'. The
        actual delta is reconciled at :meth:`commit` — over-reservation is
        forgiven (we subtract what we reserved and add what actually happened),
        under-reservation just means the commit does the real debiting.
        """
        async with self._lock:
            self._raise_if_exhausted_locked(member=member)
            self._spent.turns += turns
            self._spent.tokens += tokens
            self._spent.cost_usd += cost_usd
            logger.debug(
                "[shared_budget] reserved for %s: +turns=%d +tokens=%d +cost=%.4f → %s",
                member,
                turns,
                tokens,
                cost_usd,
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
        """Reconcile the actual turn spend against any reservation.

        Subtracts the reservation (so we don't double-count what ``reserve``
        already debited) then adds the actuals. Latches ``_exhausted`` if the
        result is at/over cap — once latched, further ``check``/``reserve``
        calls refuse, which is what stops the next member from starting.
        """
        async with self._lock:
            self._spent.turns += turns - reserved_turns
            self._spent.tokens += tokens - reserved_tokens
            self._spent.cost_usd += cost_usd - reserved_cost_usd
            # seconds is wall-clock — always recompute from monotonic, never debited.
            spent_now = self._snapshot_locked()
            logger.debug(
                "[shared_budget] committed for %s: turns=%d tokens=%d cost=%.4f → %s",
                member,
                turns,
                tokens,
                cost_usd,
                spent_now,
            )
            if self._is_over_cap_locked():
                self._exhausted = True
                logger.warning(
                    "[shared_budget] exhausted after %s's turn — capping floor: %s",
                    member,
                    spent_now,
                )

    def _raise_if_exhausted_locked(self, *, member: str) -> None:
        if self._exhausted or self._is_over_cap_locked():
            # Find the first axis that blew up — same precedence as check_budget.
            axis: str
            value: float
            limit: float
            if self._spent.turns >= self.max.max_turns:
                axis, value, limit = BudgetAxis.TURNS, self._spent.turns, self.max.max_turns
            elif self.elapsed_seconds() >= self.max.max_seconds:
                axis, value, limit = (
                    BudgetAxis.SECONDS,
                    self.elapsed_seconds(),
                    self.max.max_seconds,
                )
            elif self._spent.tokens >= self.max.max_tokens:
                axis, value, limit = BudgetAxis.TOKENS, self._spent.tokens, self.max.max_tokens
            else:
                axis, value, limit = (
                    BudgetAxis.COST_USD,
                    self._spent.cost_usd,
                    self.max.max_cost_usd,
                )
            raise BudgetExceeded(
                f"Shared floor budget exhausted on {axis} axis: "
                f"{value}/{limit} (member {member!r} blocked)",
                axis=axis,
                value=value,
                limit=limit,
                member=member,
                floor_spent=spent_to_dict(self._spent, elapsed=self.elapsed_seconds()),
            )

    def _is_over_cap_locked(self) -> bool:
        return (
            self._spent.turns >= self.max.max_turns
            or self.elapsed_seconds() >= self.max.max_seconds
            or self._spent.tokens >= self.max.max_tokens
            or self._spent.cost_usd >= self.max.max_cost_usd
        )

    def _snapshot_locked(self) -> dict[str, float | int]:
        return spent_to_dict(self._spent, elapsed=self.elapsed_seconds())


def spent_to_dict(spend: _Spend, *, elapsed: float) -> dict[str, float | int]:
    return {
        "turns": spend.turns,
        "seconds": round(elapsed, 2),
        "tokens": spend.tokens,
        "cost_usd": round(spend.cost_usd, 6),
    }
