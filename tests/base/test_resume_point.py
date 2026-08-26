"""ResumePoint — the park invariants, pinned.

The storage is three nullable fields (checkpoint compatibility); the
invariant — at most one logically active park, handoff outranking approval —
lives in AgentRun's park methods, and these tests make sure it stays there:
park twice, park in the wrong order, consume and re-park.
"""

from __future__ import annotations

from prodagent.kernel.state import (
    AgentRun,
    AwaitingApproval,
    AwaitingHandoff,
    PendingHandoff,
)
from prodagent.kernel.types import RunState, ToolCall


def _call(name: str = "search") -> ToolCall:
    return ToolCall(name=name, params={"q": "x"}, call_id="c1")


def _handoff(peer: str = "B") -> PendingHandoff:
    return PendingHandoff(peer_name=peer, task="go", message_id="mid-1")


# ------------------------------------------------------------------ reading


def test_resume_point_none_when_unparked():
    run = AgentRun(run_id="r", task="t")
    assert run.resume_point() is None


def test_resume_point_reads_approval_then_handoff():
    run = AgentRun(run_id="r", task="t")
    assert run.park_for_approval(_call(), "req-1")
    point = run.resume_point()
    assert isinstance(point, AwaitingApproval)
    assert point.request_id == "req-1"

    assert run.park_handoff(_handoff())  # a transfer outranks a pending decision
    assert isinstance(run.resume_point(), AwaitingHandoff)


# ------------------------------------------------------------------ parking


def test_park_for_approval_sets_state_and_fields():
    run = AgentRun(run_id="r", task="t")
    call = _call()

    assert run.park_for_approval(call, "req-1")

    assert run.state is RunState.SUSPENDED
    assert run.pending_tool_call is call
    assert run.pending_approval_id == "req-1"


def test_second_suspension_never_moves_the_first_parked_call():
    run = AgentRun(run_id="r", task="t")
    first = _call("first")
    assert run.park_for_approval(first, "req-1")

    assert run.park_for_approval(_call("second"), "req-2") is False
    assert run.pending_tool_call is first
    assert run.pending_approval_id == "req-1"


def test_handoff_park_refuses_when_a_handoff_is_already_parked():
    run = AgentRun(run_id="r", task="t")
    assert run.park_handoff(_handoff("B"))
    assert run.park_handoff(_handoff("C")) is False
    assert run.pending_handoff.peer_name == "B"  # first handoff wins
    assert run.pending_handoff.message_id == "mid-1"


def test_handoff_park_overwrites_an_approval_park_and_finishes_the_run():
    run = AgentRun(run_id="r", task="t")
    assert run.park_for_approval(_call(), "req-1")

    assert run.park_handoff(_handoff("B"))

    assert run.state is RunState.COMPLETED
    assert run.final_output == "Handed off to B"
    # The parked call is abandoned, not retried — the chain continues at the peer.
    assert isinstance(run.resume_point(), AwaitingHandoff)


def test_approval_park_refuses_once_a_handoff_is_parked():
    run = AgentRun(run_id="r", task="t")
    assert run.park_handoff(_handoff("B"))

    assert run.park_for_approval(_call(), "req-1") is False
    assert run.state is RunState.COMPLETED
    assert run.pending_tool_call is None


# --------------------------------------------------------------- consuming


def test_clear_approval_park_returns_the_pair_and_clears():
    run = AgentRun(run_id="r", task="t")
    call = _call()
    assert run.park_for_approval(call, "req-1")

    park = run.clear_approval_park()

    assert park == AwaitingApproval(call=call, request_id="req-1")
    assert run.pending_tool_call is None
    assert run.pending_approval_id is None
    assert run.clear_approval_park() is None  # idempotent


def test_clear_approval_park_does_not_touch_a_handoff_park():
    run = AgentRun(run_id="r", task="t")
    assert run.park_handoff(_handoff())

    assert run.clear_approval_park() is None
    assert run.pending_handoff is not None  # the relay consumes that one
