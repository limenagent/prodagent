"""BudgetLedger — reserve/commit/release and the exhausted-latch self-heal fix.

The old ``SharedBudget`` latched ``_exhausted`` permanently on any over-cap
moment, including a transient reservation that never became real spend — once
latched, the floor/chain/queue could never recover even if the reservation
was released. BudgetLedger fixes this: the over-cap test is
``committed + reserved``, so releasing an outstanding reservation can bring
the ledger back under cap and unblock subsequent checks.
"""

from __future__ import annotations

import pytest

from prodagent.base.errors import BudgetExceeded
from prodagent.kernel.budget import BudgetLedger, HardBudget


@pytest.mark.asyncio
async def test_check_passes_under_cap():
    ledger = BudgetLedger(max=HardBudget(max_turns=10, max_cost_usd=10, max_tokens=1000))
    await ledger.check(member="a")


@pytest.mark.asyncio
async def test_commit_over_cap_blocks_subsequent_check():
    ledger = BudgetLedger(max=HardBudget(max_turns=10, max_cost_usd=1.0, max_tokens=1000))
    await ledger.commit(member="a", turns=1, tokens=0, cost_usd=1.5)

    with pytest.raises(BudgetExceeded):
        await ledger.check(member="b")


@pytest.mark.asyncio
async def test_transient_reservation_overshoot_self_heals_after_release():
    """A reservation that pushes past cap must not permanently latch the ledger.

    This is the regression test for the old permanent-latch bug: reserve()
    alone used to set `_exhausted = True` forever. Here, releasing the
    reservation brings committed+reserved back under cap, and check() must
    succeed again.
    """
    ledger = BudgetLedger(max=HardBudget(max_turns=10, max_cost_usd=1.0, max_tokens=1000))

    await ledger.reserve(member="a", turns=1, cost_usd=1.0)
    with pytest.raises(BudgetExceeded):
        await ledger.reserve(member="b", turns=1, cost_usd=0.1)

    # b's reservation never happened — give it back.
    await ledger.release(member="b", reserved_turns=0, reserved_cost_usd=0.0)

    # a's own reservation reconciles down to a small real cost.
    await ledger.commit(
        member="a", turns=1, tokens=0, cost_usd=0.1, reserved_turns=1, reserved_cost_usd=1.0
    )

    await ledger.check(member="c")  # must not raise — no permanent latch


@pytest.mark.asyncio
async def test_committed_overspend_stays_permanently_blocked():
    """Unlike a released reservation, real committed overspend never un-latches."""
    ledger = BudgetLedger(max=HardBudget(max_turns=10, max_cost_usd=1.0, max_tokens=1000))

    await ledger.commit(member="a", turns=1, tokens=0, cost_usd=1.2)
    with pytest.raises(BudgetExceeded):
        await ledger.check(member="b")

    # Releasing something that was never reserved can't undo real committed spend.
    await ledger.release(member="b")
    with pytest.raises(BudgetExceeded):
        await ledger.check(member="c")


@pytest.mark.asyncio
async def test_release_is_a_noop_without_a_prior_reservation():
    ledger = BudgetLedger(max=HardBudget(max_turns=10, max_cost_usd=10, max_tokens=1000))
    await ledger.release(member="a", reserved_turns=5, reserved_cost_usd=5.0)
    assert ledger.spent.turns == 0
    assert ledger.spent.cost_usd == 0.0
