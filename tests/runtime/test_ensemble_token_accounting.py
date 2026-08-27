"""Ensemble — token accounting: a member's per-turn token spend is folded into
the shared budget's token axis, so the four-axis ceiling stays enforced for
ensemble runs (not just turns + cost)."""

from __future__ import annotations

import pytest

from prodagent.coordination.ensemble import (
    EnsembleCompletedEvent,
    EnsembleSpec,
    FloorTurn,
    FloorTurnEvent,
    SharedFloor,
    ensemble_stream,
)
from prodagent.coordination.infra.stage import MaxRounds, TerminationPolicy
from prodagent.kernel.budget import BudgetLedger, HardBudget


class _FixedTokenMember:
    """Speaks a non-empty turn carrying a fixed token cost."""

    def __init__(self, name: str, tokens: int) -> None:
        self.name = name
        self._tokens = tokens

    async def speak(self, floor: SharedFloor, *, round_num: int) -> FloorTurn:
        return FloorTurn(
            speaker=self.name,
            round=round_num,
            text=f"turn from {self.name}",
            tokens=self._tokens,
        )


@pytest.mark.asyncio
async def test_turn_tokens_are_committed_to_shared_budget():
    members = [_FixedTokenMember("alice", tokens=500), _FixedTokenMember("bob", tokens=500)]
    budget = BudgetLedger(max=HardBudget(max_turns=1_000, max_cost_usd=100, max_tokens=1_000_000))
    spec = EnsembleSpec(
        members=members,
        topic="t",
        termination=TerminationPolicy(hard_cap=MaxRounds(max_rounds=1)),  # round 0 only
        budget=budget,
    )

    turns: list[FloorTurnEvent] = []
    async for event in ensemble_stream(spec):
        if isinstance(event, FloorTurnEvent):
            turns.append(event)

    # round 0 → 2 members → 2 turns × 500 tokens committed to the shared ledger.
    assert len(turns) == 2
    assert spec.budget is not None
    assert spec.budget.spent.tokens == 1_000  # would be 0 before token accounting landed


@pytest.mark.asyncio
async def test_token_axis_ceiling_stops_the_floor():
    members = [_FixedTokenMember("alice", tokens=1_000), _FixedTokenMember("bob", tokens=1_000)]
    budget = BudgetLedger(max=HardBudget(max_turns=1_000, max_cost_usd=100, max_tokens=1_500))
    spec = EnsembleSpec(
        members=members,
        topic="t",
        termination=TerminationPolicy(hard_cap=MaxRounds(max_rounds=100)),
        budget=budget,
    )

    turns: list[FloorTurnEvent] = []
    completed: EnsembleCompletedEvent | None = None
    async for event in ensemble_stream(spec):
        if isinstance(event, FloorTurnEvent):
            turns.append(event)
        elif isinstance(event, EnsembleCompletedEvent):
            completed = event

    # alice speaks (1_000), bob speaks (2_000 cumulative → over the 1_500 token
    # cap), post-commit is_exhausted() stops the floor before a third turn.
    assert len(turns) == 2
    assert completed is not None
    assert completed.reason.reason == "budget"
    assert completed.reason.by_hard_cap is True
