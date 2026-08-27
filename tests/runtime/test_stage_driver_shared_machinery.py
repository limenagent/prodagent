"""StageDriver — the shared machinery hoisted out of the three primitives:
the reserve→act→commit budget envelope (:meth:`_run_enveloped`) and the
three dispatch modes (:meth:`_dispatch`), exercised through a minimal driver."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

import pytest

from prodagent.backends.memory.lock import InProcessLockStore
from prodagent.coordination.infra.stage import StageDriver
from prodagent.kernel.budget import BudgetLedger, HardBudget
from prodagent.ports.activation import Activation

if TYPE_CHECKING:
    from prodagent.coordination.infra.stage import TerminationReason


class _ProbeDriver(StageDriver[int]):
    """Minimal driver: no round loop of its own — tests call the shared
    machinery directly."""

    async def _rounds(self):  # pragma: no cover — not used in these tests
        return
        yield  # type: ignore[unreachable]

    def _completed(self, reason: TerminationReason) -> int:
        return 0


# ---------------------------------------------------------------------------
# _run_enveloped — reserve → act → commit / release
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_envelope_commits_actuals_and_reconciles_reservation():
    ledger = BudgetLedger(
        max=HardBudget(max_turns=10, max_seconds=60, max_tokens=10_000, max_cost_usd=10.0)
    )
    driver = _ProbeDriver()
    driver._budget = ledger

    async def act() -> tuple[int, float]:
        return (100, 0.25)

    out = await driver._run_enveloped("alice", act)

    assert out == (100, 0.25)
    assert ledger.spent.turns == 1  # reserved turn reconciled into committed
    assert ledger.spent.tokens == 100
    assert ledger.spent.cost_usd == 0.25


@pytest.mark.asyncio
async def test_envelope_blocks_member_that_cannot_reserve():
    ledger = BudgetLedger(
        max=HardBudget(max_turns=1, max_seconds=60, max_tokens=10_000, max_cost_usd=10.0)
    )
    await ledger.reserve(member="bob", turns=1)  # cap now reached
    driver = _ProbeDriver()
    driver._budget = ledger
    called = False

    async def act() -> tuple[int, float]:
        nonlocal called
        called = True
        return (1, 0.0)

    out = await driver._run_enveloped("alice", act)

    assert out is None
    assert called is False  # over-cap member never acts


@pytest.mark.asyncio
async def test_envelope_commits_the_turn_when_act_raises():
    """A crashed attempt consumes its turn slot — releasing it would let a
    crash-looping member speak forever while the turns axis reads zero."""
    ledger = BudgetLedger(
        max=HardBudget(max_turns=10, max_seconds=60, max_tokens=10_000, max_cost_usd=10.0)
    )
    driver = _ProbeDriver()
    driver._budget = ledger

    async def act() -> tuple[int, float]:
        raise RuntimeError("worker exploded")

    with pytest.raises(RuntimeError):
        await driver._run_enveloped("alice", act)

    # The turn is committed (tokens/cost unknowable → 0), not released.
    assert ledger.spent.turns == 1
    assert ledger.committed.turns == 1
    assert ledger.member_reserved("alice").turns == 0


@pytest.mark.asyncio
async def test_envelope_without_budget_just_runs():
    driver = _ProbeDriver()  # _budget stays None

    async def act() -> tuple[int, float]:
        return (5, 0.5)

    assert await driver._run_enveloped("alice", act) == (5, 0.5)


# ---------------------------------------------------------------------------
# _dispatch — serial / concurrent / single_winner
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_dispatch_serial_runs_in_order():
    driver = _ProbeDriver()
    seen: list[str] = []

    async def run_one(name: str) -> str:
        seen.append(name)
        return name.upper()

    out = await driver._dispatch(
        Activation(members=["a", "b"], dispatch="serial", label="t"), run_one
    )
    assert out == [("a", "A"), ("b", "B")]
    assert seen == ["a", "b"]


@pytest.mark.asyncio
async def test_dispatch_concurrent_keeps_member_order():
    driver = _ProbeDriver()

    async def run_one(name: str) -> str:
        return name.upper()

    out = await driver._dispatch(
        Activation(members=["a", "b", "c"], dispatch="concurrent", label="t"), run_one
    )
    assert out == [("a", "A"), ("b", "B"), ("c", "C")]


@pytest.mark.asyncio
async def test_dispatch_single_winner_only_lets_one_compute():
    driver = _ProbeDriver()
    computed: list[str] = []

    async def run_one(name: str) -> str:
        computed.append(name)
        return name

    out = await driver._dispatch(
        Activation(members=["a", "b", "c"], dispatch="single_winner", label="t"),
        run_one,
        lock_store=InProcessLockStore(),
        lock_scope="test",
    )

    winners = [name for name, result in out if result is not None]
    assert len(winners) == 1
    assert computed == winners  # losers never started computing


@pytest.mark.asyncio
async def test_dispatch_single_winner_requires_lock_store():
    driver = _ProbeDriver()

    async def run_one(name: str) -> str:
        return name

    with pytest.raises(ValueError, match="lock_store"):
        await driver._dispatch(
            Activation(members=["a"], dispatch="single_winner", label="t"), run_one
        )


# ---------------------------------------------------------------------------
# concurrent dispatch — a raising member cancels its in-flight siblings
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_dispatch_concurrent_failure_cancels_siblings():
    """A member that raises must not leave siblings burning as orphans.

    Plain ``asyncio.gather`` propagates the first exception but keeps the
    surviving coroutines running in the background — their spend never
    reaches the ledger after the run has already terminated."""
    import time

    driver = _ProbeDriver()
    slow_finished = False
    slow_cancelled = False

    async def run_one(name: str) -> str:
        nonlocal slow_finished, slow_cancelled
        if name == "boom":
            raise RuntimeError("member exploded")
        try:
            await asyncio.sleep(5.0)
            slow_finished = True
            return "slow-done"
        except asyncio.CancelledError:
            slow_cancelled = True
            raise

    started = time.monotonic()
    with pytest.raises(RuntimeError, match="member exploded"):
        await driver._dispatch(
            Activation(members=["boom", "slow"], dispatch="concurrent", label="t"),
            run_one,
        )

    assert time.monotonic() - started < 2.0  # failed fast, didn't wait out the sleeper
    assert not slow_finished
    assert slow_cancelled


# ---------------------------------------------------------------------------
# per-member reservations — release only touches your own share
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_release_cannot_free_another_members_reservation():
    ledger = BudgetLedger(
        max=HardBudget(max_turns=100, max_seconds=60, max_tokens=10_000, max_cost_usd=10.0)
    )
    await ledger.reserve(member="alice", turns=1, tokens=500)

    # Bob tries to release Alice's reservation (buggy caller / bad actor).
    await ledger.release(member="bob", reserved_turns=1, reserved_tokens=500)

    assert ledger.spent.turns == 1  # Alice's reservation is untouched
    assert ledger.spent.tokens == 500
    assert ledger.member_reserved("bob").turns == 0

    # Alice releases her own — only now does it free up.
    await ledger.release(member="alice", reserved_turns=1, reserved_tokens=500)
    assert ledger.spent.turns == 0
    assert ledger.spent.tokens == 0


@pytest.mark.asyncio
async def test_commit_reconciles_only_the_committing_members_bucket():
    ledger = BudgetLedger(
        max=HardBudget(max_turns=100, max_seconds=60, max_tokens=10_000, max_cost_usd=10.0)
    )
    await ledger.reserve(member="alice", turns=1)
    await ledger.reserve(member="bob", turns=1)

    await ledger.commit(member="alice", turns=1, tokens=10, cost_usd=0.1, reserved_turns=1)

    assert ledger.member_reserved("alice").turns == 0
    assert ledger.member_reserved("bob").turns == 1  # bob's still spoken for
    assert ledger.committed.turns == 1
    assert ledger.spent.turns == 2  # alice committed + bob reserved
