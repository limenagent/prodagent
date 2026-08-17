"""Crossing envelope — identity, direction/kind pairing, Delivery outcomes."""

from __future__ import annotations

from prodagent.runtime.coordination.messaging.envelope import (
    Crossing,
    CrossingKind,
    CrossingRejected,
    CrossingStopped,
    Delivery,
    Direction,
)


def _crossing(**overrides):
    kwargs = dict(
        direction=Direction.UPSTREAM,
        kind=CrossingKind.RESULT,
        from_agent="child",
        to="parent",
        payload={"output": "ok", "state": "completed"},
    )
    kwargs.update(overrides)
    return Crossing(message_id="m-1", **kwargs)


def test_mint_assigns_identity_and_meta():
    crossing = Crossing.mint(
        direction=Direction.DOWNSTREAM,
        kind=CrossingKind.DISPATCH,
        from_agent="parent",
        to="child",
        payload="packet",
        trace_id="run-42",
        depth=2,
    )
    assert crossing.message_id
    assert crossing.trace_id == "run-42"
    assert crossing.meta == {"depth": 2}
    assert crossing.created_at > 0


def test_mint_reuses_supplied_message_id():
    crossing = Crossing.mint(
        direction=Direction.UPSTREAM,
        kind=CrossingKind.RESULT,
        from_agent="c",
        to="p",
        payload={},
        message_id="linked-id",
    )
    assert crossing.message_id == "linked-id"


def test_directions_are_exactly_two():
    assert {d.value for d in Direction} == {"downstream", "upstream"}


def test_crossing_rejected_is_control_flow_not_error():
    rejection = CrossingRejected("contract violation: missing field", stage="contract")
    assert isinstance(rejection, CrossingStopped)
    assert rejection.strict is True
    lenient = CrossingRejected("off-shape", strict=False)
    assert lenient.strict is False


def test_delivery_reports_status_and_helpers():
    crossing = _crossing()
    assert Delivery("delivered", crossing).delivered is True
    assert Delivery("rejected", crossing, "nope").delivered is False
    assert Delivery("rejected", crossing, "nope").reason == "nope"
