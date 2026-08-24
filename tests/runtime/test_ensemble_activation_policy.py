"""Ensemble — a raw ActivationPolicy (not a speaking-order shape) drives the floor."""

from __future__ import annotations

import pytest

from prodagent.coordination.activation import Activation, ActivationContext
from prodagent.coordination.ensemble import (
    EnsembleCompletedEvent,
    EnsembleSpec,
    FloorTurnEvent,
    ensemble_stream,
)
from prodagent.coordination.floor import FloorTurn, SharedFloor
from prodagent.coordination.termination import MaxRounds, TerminationPolicy
from prodagent.kernel.budget import HardBudget, SharedBudget


class _EchoMember:
    def __init__(self, name: str) -> None:
        self.name = name

    async def speak(self, floor: SharedFloor, *, round_num: int) -> FloorTurn:
        return FloorTurn(speaker=self.name, round=round_num, text=f"turn from {self.name}")


class _SingleRoundAll:
    """Custom policy: everyone speaks concurrently once, then the floor stops."""

    async def next_activations(self, ctx: ActivationContext) -> list[Activation]:
        if ctx.round_num > 0:
            return []
        return [
            Activation(
                members=ctx.store.member_names(),
                dispatch="concurrent",
                round_num=0,
                label="custom_all_once",
            )
        ]


@pytest.mark.asyncio
async def test_raw_activation_policy_drives_the_floor():
    spec = EnsembleSpec(
        members=[_EchoMember("alice"), _EchoMember("bob")],
        topic="custom policy",
        order=_SingleRoundAll(),  # type: ignore[arg-type]
        termination=TerminationPolicy(hard_cap=MaxRounds(max_rounds=5)),
        budget=SharedBudget(
            max=HardBudget(max_turns=1_000, max_cost_usd=100, max_tokens=1_000_000)
        ),
    )
    turns: list[FloorTurnEvent] = []
    completed: EnsembleCompletedEvent | None = None
    async for event in ensemble_stream(spec):
        if isinstance(event, FloorTurnEvent):
            turns.append(event)
        else:
            completed = event
    assert completed is not None
    assert {t.turn.speaker for t in turns} == {"alice", "bob"}
    assert completed.reason.reason == "no_speaker"
