"""Business termination strategies — AllPass / BoardSatisfied / Drained.

Pure unit tests over duck-typed stores: the strategies verdict on store
shape (transcript turns, a predicate, a drained flag), compose into
TerminationPolicy.business, and never fire without their evidence.
"""

from __future__ import annotations

from prodagent.coordination.ensemble import FloorTurn
from prodagent.coordination.infra.stage import (
    AllPass,
    BoardSatisfied,
    Drained,
    MaxRounds,
    TerminationPolicy,
)


class _FakeFloor:
    """Just the shape AllPass reads: a transcript and a round count."""

    def __init__(self, turns: list[FloorTurn]) -> None:
        self.transcript = turns

    def round_count(self) -> int:
        return max((t.round for t in self.transcript), default=-1) + 1


class _FakeQueue:
    def __init__(self, drained: bool) -> None:
        self._drained = drained

    def round_count(self) -> int:
        return 0

    def is_drained(self) -> bool:
        return self._drained


def _turn(round: int, text: str = "hello") -> FloorTurn:
    return FloorTurn(speaker="a", round=round, text=text)


def test_all_pass_stops_when_last_round_was_all_pass():
    floor = _FakeFloor([_turn(0), _turn(1, text="")])  # round 1: one pass
    stop, reason = AllPass().should_stop(floor, next_round=2)
    assert stop and reason is not None and reason.reason == "convergence"
    assert not reason.by_hard_cap


def test_all_pass_no_verdict_while_round_has_substance():
    floor = _FakeFloor([_turn(0, text="real"), _turn(0, text="")])  # mixed round
    stop, reason = AllPass().should_stop(floor, next_round=1)
    assert not stop and reason is None


def test_all_pass_min_turns_guards_partial_rounds():
    floor = _FakeFloor([_turn(0), _turn(1, text="")])  # round 1 has 1 pass
    assert not AllPass(min_turns=2).should_stop(floor, next_round=2)[0]


def test_all_pass_no_verdict_without_transcript():
    queue = _FakeQueue(drained=False)
    stop, reason = AllPass().should_stop(queue, next_round=1)
    assert not stop and reason is None


def test_board_satisfied_wraps_predicate():
    satisfied = BoardSatisfied(check=lambda store: True)
    stop, reason = satisfied.should_stop(_FakeFloor([]), next_round=1)
    assert stop and reason is not None and reason.reason == "convergence"

    unsatisfied = BoardSatisfied(check=lambda store: False)
    assert not unsatisfied.should_stop(_FakeFloor([]), next_round=1)[0]


def test_drained_reports_convergence_only_when_store_reports_drained():
    assert Drained().should_stop(_FakeQueue(drained=True), next_round=1)[0]
    assert not Drained().should_stop(_FakeQueue(drained=False), next_round=1)[0]
    # Stores without is_drained get no verdict, not an error.
    assert not Drained().should_stop(_FakeFloor([_turn(0)]), next_round=1)[0]


def test_strategies_compose_into_policy_business_slot():
    policy = TerminationPolicy(hard_cap=MaxRounds(max_rounds=10), business=AllPass())
    floor = _FakeFloor([_turn(0), _turn(1, text="")])
    stop, reason = policy.should_stop(floor, next_round=2)
    assert stop and reason is not None and reason.reason == "convergence"
