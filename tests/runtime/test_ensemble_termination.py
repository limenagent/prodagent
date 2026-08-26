"""Ensemble — termination: MaxRounds hard cap, a business
TerminationStrategy, and BudgetLedger exhaustion each stop the floor
independently, using hand-rolled FloorMembers (no real Agent/LLM)."""

from __future__ import annotations

import pytest

from prodagent.coordination.ensemble import (
    EnsembleCompletedEvent,
    EnsembleSpec,
    FloorTurnEvent,
    ensemble_stream,
)
from prodagent.coordination.floor import FloorTurn, SharedFloor
from prodagent.coordination.termination import MaxRounds, TerminationPolicy
from prodagent.kernel.budget import BudgetLedger, HardBudget


class _EchoMember:
    """Always speaks a non-empty turn — never quiesces on its own, so a
    hard cap or budget is the only thing that can stop the floor."""

    def __init__(self, name: str) -> None:
        self.name = name

    async def speak(self, floor: SharedFloor, *, round_num: int) -> FloorTurn:
        return FloorTurn(speaker=self.name, round=round_num, text=f"turn from {self.name}")


@pytest.mark.asyncio
async def test_max_rounds_hard_cap_stops_an_ever_speaking_floor():
    members = [_EchoMember("alice"), _EchoMember("bob")]
    spec = EnsembleSpec(
        members=members,
        topic="t",
        termination=TerminationPolicy(hard_cap=MaxRounds(max_rounds=2)),
        budget=BudgetLedger(
            max=HardBudget(max_turns=1_000, max_cost_usd=100, max_tokens=1_000_000)
        ),
    )

    turns: list[FloorTurnEvent] = []
    completed: EnsembleCompletedEvent | None = None
    async for event in ensemble_stream(spec):
        if isinstance(event, FloorTurnEvent):
            turns.append(event)
        elif isinstance(event, EnsembleCompletedEvent):
            completed = event

    # max_rounds=2 with 2 members → 2 * 2 = 4 turns (rounds 0 and 1 only).
    assert len(turns) == 4
    assert completed is not None
    assert completed.reason.reason == "max_rounds"
    assert completed.reason.by_hard_cap is True
    assert len(completed.final_transcript) == 4


class _BusinessStopsAtTwoTurns:
    """A business TerminationStrategy that stops once the floor has 2 turns —
    proves it fires before the (much looser) hard cap."""

    def should_stop(self, floor: SharedFloor, *, next_round: int):
        from prodagent.coordination.termination import TerminationReason

        if len(floor.transcript) >= 2:
            return True, TerminationReason(reason="business_done", detail="reached 2 turns")
        return False, None


@pytest.mark.asyncio
async def test_business_strategy_stops_before_hard_cap():
    members = [_EchoMember("alice"), _EchoMember("bob")]
    spec = EnsembleSpec(
        members=members,
        topic="t",
        termination=TerminationPolicy(
            hard_cap=MaxRounds(max_rounds=100), business=_BusinessStopsAtTwoTurns()
        ),
        budget=BudgetLedger(
            max=HardBudget(max_turns=1_000, max_cost_usd=100, max_tokens=1_000_000)
        ),
    )

    turns: list[FloorTurnEvent] = []
    completed: EnsembleCompletedEvent | None = None
    async for event in ensemble_stream(spec):
        if isinstance(event, FloorTurnEvent):
            turns.append(event)
        elif isinstance(event, EnsembleCompletedEvent):
            completed = event

    assert len(turns) == 2
    assert completed is not None
    assert completed.reason.reason == "business_done"
    assert completed.reason.by_hard_cap is False


class _CostlyMember:
    """Every turn costs a fixed amount — used to exhaust a BudgetLedger."""

    def __init__(self, name: str, cost_usd: float) -> None:
        self.name = name
        self._cost_usd = cost_usd

    async def speak(self, floor: SharedFloor, *, round_num: int) -> FloorTurn:
        return FloorTurn(
            speaker=self.name,
            round=round_num,
            text=f"turn from {self.name}",
            cost_usd=self._cost_usd,
        )


@pytest.mark.asyncio
async def test_shared_budget_ceiling_stops_the_floor():
    members = [_CostlyMember("alice", cost_usd=1.0), _CostlyMember("bob", cost_usd=1.0)]
    spec = EnsembleSpec(
        members=members,
        topic="t",
        termination=TerminationPolicy(hard_cap=MaxRounds(max_rounds=100)),
        budget=BudgetLedger(
            max=HardBudget(max_turns=1_000, max_cost_usd=2.5, max_tokens=1_000_000)
        ),
    )

    turns: list[FloorTurnEvent] = []
    completed: EnsembleCompletedEvent | None = None
    async for event in ensemble_stream(spec):
        if isinstance(event, FloorTurnEvent):
            turns.append(event)
        elif isinstance(event, EnsembleCompletedEvent):
            completed = event

    # $2.5 cap, $1 per turn. The pre-speak check only blocks a turn that
    # would start *already* over cap, so turn 3 (running total $2 → $3)
    # still happens — the post-commit is_exhausted() check catches the
    # overshoot right after, stopping the floor before a 4th turn.
    assert len(turns) == 3
    assert completed is not None
    assert completed.reason.reason == "budget"
    assert completed.reason.by_hard_cap is True
