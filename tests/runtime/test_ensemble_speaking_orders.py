"""Ensemble — the two new speaking orders: Moderated (delegated judge picks,
None = discussion concluded) and FreeForAll (all members speak concurrently
every round, batch per round)."""

from __future__ import annotations

import pytest

from prodagent.kernel.budget import SharedBudget
from prodagent.coordination.ensemble import (
    EnsembleCompletedEvent,
    EnsembleSpec,
    FloorTurnEvent,
    FreeForAll,
    Moderated,
    ensemble_stream,
)
from prodagent.coordination.floor import FloorTurn, SharedFloor
from prodagent.coordination.termination import MaxRounds, TerminationPolicy
from prodagent.kernel.budget import HardBudget


class _EchoMember:
    def __init__(self, name: str) -> None:
        self.name = name

    async def speak(self, floor: SharedFloor, *, round_num: int) -> FloorTurn:
        return FloorTurn(speaker=self.name, round=round_num, text=f"turn from {self.name}")


def _budget() -> SharedBudget:
    return SharedBudget(max=HardBudget(max_turns=1_000, max_cost_usd=100, max_tokens=1_000_000))


async def _run(spec: EnsembleSpec) -> tuple[list[FloorTurnEvent], EnsembleCompletedEvent]:
    turns: list[FloorTurnEvent] = []
    completed: EnsembleCompletedEvent | None = None
    async for event in ensemble_stream(spec):
        if isinstance(event, FloorTurnEvent):
            turns.append(event)
        else:
            completed = event
    assert completed is not None
    return turns, completed


@pytest.mark.asyncio
async def test_moderated_picker_decides_order_and_none_concludes():
    """The picker picks bob first (not round-robin order), then alice, then
    None — the floor stops with the graceful `no_speaker` reason."""
    picks = ["bob", "alice", "bob", None]

    async def picker(floor: SharedFloor) -> str | None:
        return picks[len(floor.transcript)]

    spec = EnsembleSpec(
        members=[_EchoMember("alice"), _EchoMember("bob")],
        topic="t",
        order=Moderated(picker=picker),
        termination=TerminationPolicy(hard_cap=MaxRounds(max_rounds=10)),
        budget=_budget(),
    )
    turns, completed = await _run(spec)

    assert [t.turn.speaker for t in turns] == ["bob", "alice", "bob"]
    assert completed.reason.reason == "no_speaker"
    assert completed.reason.by_hard_cap is False


@pytest.mark.asyncio
async def test_moderated_round_wraps_when_picker_revisits_a_speaker():
    """bob, alice, bob — bob speaks in round 0, alice round 0, then bob again
    has *already* spoken in round 0 → round 1. max_rounds=1 must therefore
    stop the floor after exactly the first two turns."""
    picks = ["bob", "alice", "bob", "bob"]

    async def picker(floor: SharedFloor) -> str | None:
        return picks[len(floor.transcript)]

    spec = EnsembleSpec(
        members=[_EchoMember("alice"), _EchoMember("bob")],
        topic="t",
        order=Moderated(picker=picker),
        termination=TerminationPolicy(hard_cap=MaxRounds(max_rounds=1)),
        budget=_budget(),
    )
    turns, completed = await _run(spec)

    assert [t.turn.speaker for t in turns] == ["bob", "alice"]
    assert [t.turn.round for t in turns] == [0, 0]
    assert completed.reason.reason == "max_rounds"
    assert completed.reason.by_hard_cap is True


@pytest.mark.asyncio
async def test_free_for_all_runs_every_member_concurrently_per_round():
    """Three members, max_rounds=2 → 2 batches × 3 concurrent speaks = 6 turns;
    each batch is one round."""
    spec = EnsembleSpec(
        members=[_EchoMember("a"), _EchoMember("b"), _EchoMember("c")],
        topic="t",
        order=FreeForAll(),
        termination=TerminationPolicy(hard_cap=MaxRounds(max_rounds=2)),
        budget=_budget(),
    )
    turns, completed = await _run(spec)

    assert len(turns) == 6
    # Turns land in member order within each batch (deterministic stream).
    assert [t.turn.speaker for t in turns] == ["a", "b", "c", "a", "b", "c"]
    assert [t.turn.round for t in turns] == [0, 0, 0, 1, 1, 1]
    assert completed.reason.reason == "max_rounds"
    assert len(completed.final_transcript) == 6


@pytest.mark.asyncio
async def test_free_for_all_actually_overlaps_speaks():
    """Prove concurrency: each member waits on a shared barrier inside speak().
    Under serial dispatch this would deadlock (first member waits forever);
    under concurrent dispatch all three arrive and the barrier releases."""
    import asyncio

    barrier = asyncio.Barrier(3)

    class _SyncMember:
        def __init__(self, name: str) -> None:
            self.name = name

        async def speak(self, floor: SharedFloor, *, round_num: int) -> FloorTurn:
            await barrier.wait()
            return FloorTurn(speaker=self.name, round=round_num, text="ok")

    spec = EnsembleSpec(
        members=[_SyncMember("a"), _SyncMember("b"), _SyncMember("c")],
        topic="t",
        order=FreeForAll(),
        termination=TerminationPolicy(hard_cap=MaxRounds(max_rounds=1)),
        budget=_budget(),
    )
    turns, completed = await _run(spec)

    assert len(turns) == 3
    assert completed.reason.reason == "max_rounds"
