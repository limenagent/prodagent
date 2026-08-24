"""StageDriver — the shared machinery hoisted out of the three primitives:
the reserve→act→commit budget envelope (:meth:`_run_enveloped`) and the
three dispatch modes (:meth:`_dispatch`), exercised through a minimal driver."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from prodagent.backends.memory.lock import InProcessLockStore
from prodagent.coordination._stage import StageDriver
from prodagent.coordination.activation import Activation
from prodagent.kernel.budget import BudgetLedger, HardBudget

if TYPE_CHECKING:
    from prodagent.coordination.termination import TerminationReason


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
async def test_envelope_releases_reservation_when_act_raises():
    ledger = BudgetLedger(
        max=HardBudget(max_turns=10, max_seconds=60, max_tokens=10_000, max_cost_usd=10.0)
    )
    driver = _ProbeDriver()
    driver._budget = ledger

    async def act() -> tuple[int, float]:
        raise RuntimeError("worker exploded")

    with pytest.raises(RuntimeError):
        await driver._run_enveloped("alice", act)

    # The reservation was given back: the ledger is not permanently over-drawn.
    assert ledger.spent.turns == 0
    await ledger.check(member="alice")  # no BudgetExceeded


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
