"""Blackboard — event-mode triggers: matching experts write concurrently, no
arbitration needed since they target disjoint keys.
"""

from __future__ import annotations

import pytest

from prodagent.runtime.coordination.blackboard import (
    BlackboardCompletedEvent,
    BlackboardSpec,
    Board,
    BoardWrite,
    BoardWriteEvent,
    Trigger,
    blackboard_stream,
)


class _WriteOnceExpert:
    def __init__(self, name: str, key: str) -> None:
        self.name = name
        self._key = key
        self.calls = 0

    async def try_contribute(self, board: Board, *, trigger: Trigger) -> BoardWrite | None:
        self.calls += 1
        if board.version_of(self._key) > 0:
            return None  # already contributed — pass on subsequent rounds
        return BoardWrite(key=self._key, value=f"value-from-{self.name}", author=self.name)


@pytest.mark.asyncio
async def test_event_mode_lets_disjoint_experts_write_concurrently_then_quiesces():
    alice = _WriteOnceExpert("alice", "field_a")
    bob = _WriteOnceExpert("bob", "field_b")

    spec = BlackboardSpec(
        experts={"alice": alice, "bob": bob},
        triggers={
            "kickoff": Trigger(name="kickoff", keys=[], experts=["alice", "bob"], mode="event"),
        },
    )

    write_events: list[BoardWriteEvent] = []
    completed: BlackboardCompletedEvent | None = None
    async for event in blackboard_stream(spec):
        if isinstance(event, BoardWriteEvent):
            write_events.append(event)
        else:
            completed = event

    written_keys = {e.write.key for e in write_events}
    assert written_keys == {"field_a", "field_b"}
    assert completed is not None
    assert completed.reason.reason == "no_contribution"
    slots = completed.board_snapshot["slots"]
    assert slots["field_a"]["value"] == "value-from-alice"
    assert slots["field_b"]["value"] == "value-from-bob"


class _PassingExpert:
    """Never contributes — used to prove a quiescent board stops the loop."""

    def __init__(self, name: str) -> None:
        self.name = name

    async def try_contribute(self, board: Board, *, trigger: Trigger) -> BoardWrite | None:
        return None


@pytest.mark.asyncio
async def test_no_matching_trigger_on_first_round_is_quiescent_not_an_error():
    expert = _PassingExpert("idle")
    spec = BlackboardSpec(
        experts={"idle": expert},
        # keys=["never.*"] never matches on round 0 (no prior writes exist to match against).
        triggers={"only": Trigger(name="only", keys=["never.*"], experts=["idle"], mode="event")},
    )

    completed: BlackboardCompletedEvent | None = None
    async for event in blackboard_stream(spec):
        if isinstance(event, BlackboardCompletedEvent):
            completed = event

    assert completed is not None
    assert completed.reason.reason == "quiescent"
