"""Budget — the ceiling (HardBudget), the stateless check, and the shared Ledger.

One ceiling vocabulary everywhere: a lone agent checks its own spend with
:func:`check_budget`; concurrent spenders (spawn children, peer chains,
ensembles) share one :class:`BudgetLedger` by reference and reserve/commit
against it.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Protocol

from prodagent.core.budget_axes import evaluate_axes
from prodagent.core.exceptions import BudgetExceeded

if TYPE_CHECKING:
    from prodagent.kernel.state import AgentRun

logger = logging.getLogger(__name__)


@dataclass
class HardBudget:
    """Conservative defaults: unattended runs fail fast rather than burning quota."""

    max_turns: int = 20
    max_seconds: float = 120.0
    max_tokens: int = 100_000
    max_cost_usd: float = 1.0


SAFETY_NET_BUDGET = HardBudget()


def check_budget(
    run: AgentRun,
    budget: HardBudget,
    *,
    extra_turns: int = 0,
    extra_tokens: int = 0,
    extra_cost_usd: float = 0.0,
) -> None:
    turn_count = run.turn_count + extra_turns
    elapsed = run.elapsed_seconds()
    total_tokens = run.input_tokens + run.output_tokens + extra_tokens
    billable_tokens = total_tokens - run.cache_read_tokens
    cost_usd = run.cost_usd + extra_cost_usd

    outcome = evaluate_axes(
        turns=turn_count,
        elapsed=elapsed,
        tokens=billable_tokens,
        cost_usd=cost_usd,
        max_turns=budget.max_turns,
        max_seconds=budget.max_seconds,
        max_tokens=budget.max_tokens,
        max_cost_usd=budget.max_cost_usd,
    )
    if outcome is None:
        return
    axis, value, limit = outcome

    if axis == "turns":
        raise BudgetExceeded(
            f"Turn limit reached: {turn_count}/{budget.max_turns}",
            run_id=run.run_id,
            axis="turns",
            value=value,
            limit=limit,
        )
    if axis == "seconds":
        raise BudgetExceeded(
            f"Time limit reached: {elapsed:.1f}s/{budget.max_seconds}s",
            run_id=run.run_id,
            axis="seconds",
            value=value,
            limit=limit,
        )
    if axis == "tokens":
        cached = run.cache_read_tokens
        raise BudgetExceeded(
            f"Token limit reached: {billable_tokens}/{budget.max_tokens}"
            + (f" ({cached} cached tokens excluded)" if cached else ""),
            run_id=run.run_id,
            axis="tokens",
            value=value,
            limit=limit,
        )
    raise BudgetExceeded(
        f"Cost limit reached: ${cost_usd:.4f}/${budget.max_cost_usd}",
        run_id=run.run_id,
        axis="cost_usd",
        value=value,
        limit=limit,
    )


# ── Shared ledger ─────────────────────────────────────────────────────────────
# Moved verbatim from coordination/budget_ledger.py.


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
    unit of work starts (optional — skip if the spender can't estimate ahead),
    :meth:`commit` after it finishes with the real cost, :meth:`release` if a
    reservation never turned into real work. Reservations are tracked per
    member — :meth:`release` can only give back what the named member itself
    reserved. ``seconds`` is wall-clock since construction — never
    reserved/committed/released like the other three axes.
    """

    max: HardBudget
    """The ceiling — turns/seconds/tokens/cost."""

    _committed: _Spend = field(default_factory=_Spend)
    """Permanent — only moved by commit()'s actual deltas. Never decreases."""

    _reserved: _Spend = field(default_factory=_Spend)
    """Transient — moved by reserve()/release(), reconciled away by commit()."""

    _reserved_by: dict[str, _Spend] = field(default_factory=dict)
    """Per-member reservation buckets — sum never exceeds ``_reserved``."""

    _lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    _start_monotonic: float = field(default_factory=time.monotonic)

    @property
    def committed(self) -> _Spend:
        """Read-only snapshot of settled spend only. Executors check against
        this view: an in-flight reservation gates siblings at reserve time,
        and must not block the very child it was reserved for."""
        return _Spend(
            turns=self._committed.turns,
            tokens=self._committed.tokens,
            cost_usd=self._committed.cost_usd,
        )

    @property
    def spent(self) -> _Spend:
        """Read-only snapshot of current (committed + reserved) spend — best-effort, no lock."""
        return _Spend(
            turns=self._committed.turns + self._reserved.turns,
            tokens=self._committed.tokens + self._reserved.tokens,
            cost_usd=self._committed.cost_usd + self._reserved.cost_usd,
        )

    def member_reserved(self, member: str) -> _Spend:
        """Read-only snapshot of one member's outstanding reservation."""
        bucket = self._reserved_by.get(member)
        return (
            _Spend(turns=bucket.turns, tokens=bucket.tokens, cost_usd=bucket.cost_usd)
            if bucket
            else _Spend()
        )

    def elapsed_seconds(self) -> float:
        """Wall-clock since this BudgetLedger was created — the 'seconds' axis."""
        return time.monotonic() - self._start_monotonic

    def is_exhausted(self) -> bool:
        """Cheap lock-free pre-check — pollable between rounds/hops.
        Authoritative check (raises with axis detail) is :meth:`check`."""
        return self._is_over_cap_locked()

    async def check(self, *, member: str) -> None:
        """Raise :class:`BudgetExceeded` if committed+reserved is at/over cap.
        Called before a unit of work starts. Does not debit."""
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
        """Pre-debit an estimate before a unit of work starts. Optional — use
        when a spender can estimate ahead and wants concurrent siblings to see
        this work as "already spoken for". Raises (without debiting) if at/over
        cap. Reconciled away by :meth:`commit` or undone by :meth:`release`."""
        async with self._lock:
            self._raise_if_over_cap_locked(member=member)
            self._reserved.turns += turns
            self._reserved.tokens += tokens
            self._reserved.cost_usd += cost_usd
            bucket = self._reserved_by.setdefault(member, _Spend())
            bucket.turns += turns
            bucket.tokens += tokens
            bucket.cost_usd += cost_usd
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
        """Give back a reservation that never became real spend (losing lock-race
        bid, requeued task). Never touches ``_committed``; can only reduce the
        named member's *own* outstanding reservation — one member must not be
        able to free another's spoken-for share — and can only help a
        subsequent :meth:`check`."""
        async with self._lock:
            bucket = self._reserved_by.get(member)
            if bucket is None:
                return
            d_turns = min(reserved_turns, bucket.turns)
            d_tokens = min(reserved_tokens, bucket.tokens)
            d_cost = min(reserved_cost_usd, bucket.cost_usd)
            bucket.turns -= d_turns
            bucket.tokens -= d_tokens
            bucket.cost_usd -= d_cost
            self._reserved.turns = max(0, self._reserved.turns - d_turns)
            self._reserved.tokens = max(0, self._reserved.tokens - d_tokens)
            self._reserved.cost_usd = max(0.0, self._reserved.cost_usd - d_cost)
            logger.debug(
                "[budget_ledger] released for %s: -turns=%d -tokens=%d -cost=%.4f → %s",
                member,
                d_turns,
                d_tokens,
                d_cost,
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
        """Reconcile a reservation (if any) against actual spend and commit it.
        Removes the reservation (no double-count) and adds actual deltas to
        ``_committed``. If the overshoot came from *other* outstanding
        reservations rather than this commit's actual spend, a later
        :meth:`release` by the reserving member can bring the total back under
        cap (self-heal)."""
        async with self._lock:
            self._reserved.turns = max(0, self._reserved.turns - reserved_turns)
            self._reserved.tokens = max(0, self._reserved.tokens - reserved_tokens)
            self._reserved.cost_usd = max(0.0, self._reserved.cost_usd - reserved_cost_usd)
            bucket = self._reserved_by.get(member)
            if bucket is not None:
                bucket.turns = max(0, bucket.turns - reserved_turns)
                bucket.tokens = max(0, bucket.tokens - reserved_tokens)
                bucket.cost_usd = max(0.0, bucket.cost_usd - reserved_cost_usd)
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
        outcome = self._evaluate_axes_locked()
        if outcome is None:
            return
        axis, value, limit = outcome
        raise BudgetExceeded(
            f"Shared budget ledger exhausted on {axis} axis: "
            f"{value}/{limit} (member {member!r} blocked)",
            axis=axis,
            value=value,
            limit=limit,
            member=member,
            floor_spent=spent_to_dict(self.spent, elapsed=self.elapsed_seconds()),
        )

    def _is_over_cap_locked(self) -> bool:
        return self._evaluate_axes_locked() is not None

    def _evaluate_axes_locked(self) -> tuple[str, float, float] | None:
        return evaluate_axes(
            turns=self._committed.turns + self._reserved.turns,
            elapsed=self.elapsed_seconds(),
            tokens=self._committed.tokens + self._reserved.tokens,
            cost_usd=self._committed.cost_usd + self._reserved.cost_usd,
            max_turns=self.max.max_turns,
            max_seconds=self.max.max_seconds,
            max_tokens=self.max.max_tokens,
            max_cost_usd=self.max.max_cost_usd,
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


class SpendSnapshot(Protocol):
    """Structural: anything carrying live spend totals (SpawnAccumulator)."""

    turns: int
    input_tokens: int
    output_tokens: int
    cost_usd: float


def check_spawn_budget(
    run: AgentRun,
    budget: HardBudget | None,
    ledger: BudgetLedger | None = None,
) -> None:
    """The ledger is the single enforcement source: children commit live,
    peers commit at handoff. The run's own spend (metrics) plus the ledger's
    SETTLED spend must fit the ceiling — in-flight reservations are the
    reserve gate's business, not the executor's. The fold accumulator is
    reporting-only: folded child spend is already in the ledger."""
    if budget is None:
        return
    extra_turns = 0
    extra_tokens = 0
    extra_cost_usd = 0.0
    if ledger is not None:
        settled = ledger.committed
        extra_turns = settled.turns
        extra_tokens = settled.tokens
        extra_cost_usd = settled.cost_usd
    check_budget(
        run,
        budget,
        extra_turns=extra_turns,
        extra_tokens=extra_tokens,
        extra_cost_usd=extra_cost_usd,
    )
