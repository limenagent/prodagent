"""Blackboard — termination: MaxRounds hard cap, terminal_check, and the
BudgetLedger ceiling all stop the loop independently of each other."""

from __future__ import annotations

import pytest

from prodagent.coordination.blackboard import (
    BlackboardCompletedEvent,
    BlackboardSpec,
    Board,
    BoardWrite,
    BoardWriteEvent,
    Trigger,
    blackboard_stream,
)
from prodagent.coordination.infra.stage import MaxRounds, TerminationPolicy
from prodagent.kernel.budget import BudgetLedger, HardBudget


class _CounterExpert:
    """Always contributes — never quiesces on its own, so MaxRounds/budget is
    the only thing that can stop the loop."""

    def __init__(self, name: str, key: str) -> None:
        self.name = name
        self._key = key

    async def try_contribute(self, board: Board, *, trigger: Trigger) -> BoardWrite | None:
        current = board.read([self._key]).get(self._key, 0)
        return BoardWrite(key=self._key, value=current + 1, author=self.name)


@pytest.mark.asyncio
async def test_max_rounds_hard_cap_stops_an_ever_contributing_board():
    expert = _CounterExpert("ticker", "count")
    spec = BlackboardSpec(
        experts={"ticker": expert},
        triggers={"always": Trigger(name="always", keys=[], experts=["ticker"], mode="event")},
        termination=TerminationPolicy(hard_cap=MaxRounds(max_rounds=3)),
    )

    write_events: list[BoardWriteEvent] = []
    completed: BlackboardCompletedEvent | None = None
    async for event in blackboard_stream(spec):
        if isinstance(event, BoardWriteEvent):
            write_events.append(event)
        else:
            completed = event

    assert len(write_events) == 3
    assert completed is not None
    assert completed.reason.reason == "max_rounds"
    assert completed.reason.by_hard_cap is True


@pytest.mark.asyncio
async def test_terminal_check_stops_before_max_rounds():
    expert = _CounterExpert("ticker", "count")
    spec = BlackboardSpec(
        experts={"ticker": expert},
        triggers={"always": Trigger(name="always", keys=[], experts=["ticker"], mode="event")},
        termination=TerminationPolicy(hard_cap=MaxRounds(max_rounds=100)),
        terminal_check=lambda board: board.read(["count"]).get("count", 0) >= 2,
    )

    write_events: list[BoardWriteEvent] = []
    completed: BlackboardCompletedEvent | None = None
    async for event in blackboard_stream(spec):
        if isinstance(event, BoardWriteEvent):
            write_events.append(event)
        else:
            completed = event

    assert len(write_events) == 2
    assert completed is not None
    assert completed.reason.reason == "terminal_check"


@pytest.mark.asyncio
async def test_budget_ledger_ceiling_stops_the_board():
    expert = _CounterExpert("ticker", "count")
    ledger = BudgetLedger(max=HardBudget(max_turns=2, max_cost_usd=100, max_tokens=1_000_000))
    spec = BlackboardSpec(
        experts={"ticker": expert},
        triggers={"always": Trigger(name="always", keys=[], experts=["ticker"], mode="event")},
        termination=TerminationPolicy(hard_cap=MaxRounds(max_rounds=100)),
        budget=ledger,
    )

    write_events: list[BoardWriteEvent] = []
    completed: BlackboardCompletedEvent | None = None
    async for event in blackboard_stream(spec):
        if isinstance(event, BoardWriteEvent):
            write_events.append(event)
        else:
            completed = event

    # Budget caps turns at 2 — the 3rd round's sole expert can't reserve a
    # turn, produces no write, and the board reports no_contribution.
    assert len(write_events) == 2
    assert completed is not None
    assert completed.reason.reason == "no_contribution"


@pytest.mark.asyncio
async def test_version_conflict_write_is_not_silently_swallowed():
    from prodagent.coordination.blackboard import BoardVersionConflict

    board = Board()
    await board.write("k", "v1")
    with pytest.raises(BoardVersionConflict):
        await board.write("k", "v2", expected_version=0)
    # Correct expected_version succeeds.
    new_version = await board.write("k", "v2", expected_version=1)
    assert new_version == 2
    assert board.read(["k"])["k"] == "v2"
