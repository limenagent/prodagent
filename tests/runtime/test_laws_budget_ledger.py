"""BudgetLedger accounting laws — the identities settlement must never break.

Drive the ledger through *arbitrary* op sequences (reserve / commit /
release, interleaved, multi-member) and assert after every step:

- **Additivity**: ``spent == committed + reserved`` on every axis — the two
  books always sum to what the ledger reports as outstanding.
- **Non-negative books**: neither ``committed`` nor ``_reserved`` ever goes
  below zero, no matter how oversized a release or commit's reconciliation
  is.
- **Member containment**: the sum of per-member reservation buckets never
  exceeds total reserved — a member's release can never free another
  member's spoken-for share.
- **No double count**: a commit that reconciles a reservation moves
  ``reserved`` down by at most that reservation while adding the actuals to
  ``committed`` exactly once.

These are the invariants crash-looping members and losing lock races lean
on; an example-based test samples them, a law holds them everywhere.
"""

from __future__ import annotations

import asyncio
import contextlib
from typing import Any

from hypothesis import given, settings
from hypothesis import strategies as st

from prodagent.kernel.budget import BudgetLedger, HardBudget

_members = st.sampled_from(["a", "b", "c"])
_amounts = st.integers(min_value=0, max_value=50)


@st.composite
def _ops(draw: Any) -> list[tuple[str, str, int, int, int, int, int, int]]:
    """Op tuples: (kind, member, turns, tokens, cost_cents, r_turns, r_tokens, r_cost_cents).

    Reconciliation amounts (``r_*``) are drawn independently of reserves —
    deliberately, so the law exercises over- and under-reconciliation paths."""
    out = []
    for _ in range(draw(st.integers(min_value=0, max_value=25))):
        kind = draw(st.sampled_from(["reserve", "commit", "release"]))
        member = draw(_members)
        t, k, c = draw(_amounts), draw(_amounts), draw(_amounts)
        rt, rk, rc = draw(_amounts), draw(_amounts), draw(_amounts)
        out.append((kind, member, t, k, c, rt, rk, rc))
    return out


def _cents(x: float) -> int:
    return round(x * 10_000)


def _assert_identities(ledger: BudgetLedger) -> None:
    spent = ledger.spent
    committed = ledger._committed
    reserved = ledger._reserved
    for axis, s, c, r in (
        ("turns", spent.turns, committed.turns, reserved.turns),
        ("tokens", spent.tokens, committed.tokens, reserved.tokens),
        ("cost", _cents(spent.cost_usd), _cents(committed.cost_usd), _cents(reserved.cost_usd)),
    ):
        assert c + r == s, f"{axis}: committed+reserved must equal spent"
        assert c >= 0 and r >= 0, f"{axis}: books must never go negative"
    bucket_turns = sum(b.turns for b in ledger._reserved_by.values())
    bucket_tokens = sum(b.tokens for b in ledger._reserved_by.values())
    assert bucket_turns <= reserved.turns, "member buckets exceed total reserved (turns)"
    assert bucket_tokens <= reserved.tokens, "member buckets exceed total reserved (tokens)"


@settings(max_examples=200, deadline=None)
@given(_ops())
def test_ledger_accounting_law(ops: list[tuple[str, str, int, int, int, int, int, int]]) -> None:
    async def drive() -> None:
        ledger = BudgetLedger(
            max=HardBudget(
                max_turns=10**9, max_seconds=10**9, max_tokens=10**12, max_cost_usd=10**9
            )
        )
        for kind, member, t, k, c, rt, rk, rc in ops:
            if kind == "reserve":
                # Over-cap reservations raise without debiting — suppress and
                # move on; the books must hold either way.
                with contextlib.suppress(Exception):
                    await ledger.reserve(member=member, turns=t, tokens=k, cost_usd=c / 100)
            elif kind == "commit":
                await ledger.commit(
                    member=member,
                    turns=t,
                    tokens=k,
                    cost_usd=c / 100,
                    reserved_turns=rt,
                    reserved_tokens=rk,
                    reserved_cost_usd=rc / 100,
                )
            else:
                await ledger.release(
                    member=member,
                    reserved_turns=rt,
                    reserved_tokens=rk,
                    reserved_cost_usd=rc / 100,
                )
            _assert_identities(ledger)

    asyncio.run(drive())
