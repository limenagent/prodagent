"""Blackboard — buzz_in mode: lock-first-then-compute arbitration.

Confirmed semantics (AskUserQuestion, this round): candidates race for a lock
*before* computing anything; the loser's coroutine returns without ever
calling into the expert. This is not race-to-answer-with-cancellation — there
is no in-flight computation to cancel, because losers never start.
"""

from __future__ import annotations

import asyncio

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


class _BuzzInCandidate:
    """Tracks whether it ever actually started computing (not just was asked)."""

    def __init__(self, name: str) -> None:
        self.name = name
        self.compute_started = 0

    async def try_contribute(self, board: Board, *, trigger: Trigger) -> BoardWrite | None:
        self.compute_started += 1
        # Simulate real work — long enough that if two candidates both got
        # past the lock, both would show compute_started == 1 well before
        # either finishes.
        await asyncio.sleep(0.02)
        return BoardWrite(key="answer", value=self.name, author=self.name)


@pytest.mark.asyncio
async def test_exactly_one_buzz_in_candidate_ever_starts_computing():
    candidates = [_BuzzInCandidate(f"expert_{i}") for i in range(6)]
    experts = {c.name: c for c in candidates}

    spec = BlackboardSpec(
        experts=experts,
        triggers={
            "buzzer": Trigger(name="buzzer", keys=[], experts=list(experts), mode="buzz_in"),
        },
        terminal_check=lambda board: board.version_of("answer") > 0,
    )

    write_events: list[BoardWriteEvent] = []
    completed: BlackboardCompletedEvent | None = None
    async for event in blackboard_stream(spec):
        if isinstance(event, BoardWriteEvent):
            write_events.append(event)
        else:
            completed = event

    started = [c for c in candidates if c.compute_started > 0]
    assert len(started) == 1, (
        f"expected exactly one candidate to ever start computing, got {[c.name for c in started]}"
    )
    assert len(write_events) == 1
    assert write_events[0].write.author == started[0].name
    assert completed is not None
    slots = completed.board_snapshot["slots"]
    assert slots["answer"]["value"] == started[0].name


class _AlwaysReadyCandidate:
    """A buzz_in candidate that keeps wanting to answer every round —
    used to prove the trigger's lock is released after each round and can
    be re-acquired fresh the next round (not held forever by the first
    winner)."""

    def __init__(self, name: str, counter_key: str) -> None:
        self.name = name
        self._counter_key = counter_key
        self.compute_started = 0

    async def try_contribute(self, board: Board, *, trigger: Trigger) -> BoardWrite | None:
        self.compute_started += 1
        current = board.read([self._counter_key]).get(self._counter_key, 0)
        if current >= 3:
            return None
        return BoardWrite(key=self._counter_key, value=current + 1, author=self.name)


@pytest.mark.asyncio
async def test_buzz_in_lock_is_reacquired_fresh_each_round():
    a = _AlwaysReadyCandidate("a", "counter")
    b = _AlwaysReadyCandidate("b", "counter")

    spec = BlackboardSpec(
        experts={"a": a, "b": b},
        # keys=[] matches every round regardless of what changed, so the same
        # lock name is raced for again each round.
        triggers={
            "counter_trigger": Trigger(
                name="counter_trigger", keys=[], experts=["a", "b"], mode="buzz_in"
            ),
        },
    )

    write_events: list[BoardWriteEvent] = []
    async for event in blackboard_stream(spec):
        if isinstance(event, BoardWriteEvent):
            write_events.append(event)

    # Counter climbs from 0 to 3 across multiple rounds — proves the lock
    # doesn't stay permanently held by whoever won the first round.
    counter_values = [e.write.value for e in write_events if e.write.key == "counter"]
    assert counter_values == [1, 2, 3]
