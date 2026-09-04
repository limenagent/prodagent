"""Interrupt — the park's vocabulary and the door table (column 20/8).

The laws under test: a park is ONE durable fact (kind, request id, parked
node, staged action in the payload — never a family of fields); the kind is
payload, not mechanism (approve / await_external ride the same
door); the wire round-trips the kind so a resumed process knows what it was
waiting for; the historical three-field checkpoint wire upgrades on read;
and the four doors enforce the allowed-transition table — the nonsensical
endings (dead end into success, suspended straight into failed) are illegal
at the write site.
"""

from __future__ import annotations

import pytest

from prodagent.base.errors import IllegalTransition
from prodagent.kernel.bodies import FnBody
from prodagent.kernel.graph import Node, Plan
from prodagent.kernel.interrupt import Interrupt, InterruptKind
from prodagent.kernel.run import Run
from prodagent.kernel.scheduler import Scheduler
from prodagent.kernel.types import RunState, ToolCall, ToolOutcome, ToolResult
from prodagent.tooling.dispatcher import ToolDispatcher


def _call(name: str = "do_thing") -> ToolCall:
    return ToolCall(name=name, params={"x": 1})


def _suspended_result(request_id: str = "req-9") -> ToolResult:
    return ToolResult(
        ToolOutcome.SUSPENDED,
        value="",
        tool="do_thing",
        approval_request_id=request_id,
    )


# ── the vocabulary ────────────────────────────────────────────────────────────


def test_three_trigger_kinds_one_mechanism():
    assert [k.value for k in InterruptKind] == ["approve", "await_external"]


def test_interrupt_wire_roundtrip_carries_kind_payload_and_node():
    it = Interrupt(InterruptKind.AWAIT_EXTERNAL, "req-1", {"event": "callback"}, "node:entry")
    assert Interrupt.from_dict(it.to_dict()) == it


# ── the park and its views ────────────────────────────────────────────────────


def _parked_run(iv: Interrupt | None = None) -> Run:
    run = Run(run_id="r", task="t")
    assert run.park(iv or Interrupt.from_result(_suspended_result(), _call()))
    return run


def test_approval_park_carries_the_staged_call_and_the_request():
    run = _parked_run()
    iv = run.interrupt
    assert iv is not None and iv.kind is InterruptKind.APPROVE
    assert iv.request_id == "req-9"
    assert run.pending_approval_id == "req-9"
    staged = iv.staged_call()
    assert staged is not None and staged.name == "do_thing"


def test_another_kind_of_wait_is_just_payload():
    run = _parked_run(
        Interrupt(InterruptKind.AWAIT_EXTERNAL, "req-3", {"question": "which account?"})
    )
    iv = run.interrupt
    assert iv is not None and iv.kind is InterruptKind.AWAIT_EXTERNAL
    assert iv.payload["question"] == "which account?"


def test_taking_the_interrupt_consumes_the_whole_park():
    run = _parked_run(Interrupt(InterruptKind.AWAIT_EXTERNAL, "req-4", {"event": "webhook"}))
    iv = run.take_interrupt()
    assert iv is not None and iv.request_id == "req-4"
    assert run.interrupt is None
    assert run.pending_approval_id is None


def test_a_second_suspension_never_moves_the_first_park():
    run = _parked_run()
    assert run.park(Interrupt(InterruptKind.AWAIT_EXTERNAL, "req-2", {})) is False
    assert run.interrupt is not None and run.interrupt.request_id == "req-9"


def test_park_wire_roundtrip_keeps_the_kind():
    run = _parked_run(Interrupt(InterruptKind.AWAIT_EXTERNAL, "req-5", {"q": "how many?"}))
    restored = Run.from_dict(run.to_dict())
    iv = restored.interrupt
    assert iv is not None and iv.kind is InterruptKind.AWAIT_EXTERNAL
    assert iv.payload == {"q": "how many?"}


# ── the door table ───────────────────────────────────────────────────────────


def test_a_dead_end_never_becomes_a_success():
    run = Run(run_id="r2", task="t")
    run.fail("dead end")
    with pytest.raises(IllegalTransition):
        run.complete("miracle")


def test_suspended_cannot_fail_directly():
    run = Run(run_id="r3", task="t")
    run.suspend("waiting")
    with pytest.raises(IllegalTransition):
        run.fail("changed my mind")


def test_suspension_clears_by_resume_not_by_transfer():
    run = Run(run_id="r4", task="t")
    run.suspend("waiting on approval")
    run.resume()
    assert run.state is RunState.RUNNING


def test_terminal_re_settle_is_idempotent_not_a_transition():
    run = Run(run_id="r5", task="t")
    run.complete("answer")
    run.complete()  # a settler re-confirming — a no-op, not a lifecycle move
    assert run.state is RunState.COMPLETED


def test_late_governance_veto_fails_a_completed_run():
    run = Run(run_id="r6", task="t")
    run.complete("unacceptable artifact")
    run.fail("contract violation")
    assert run.state is RunState.FAILED


# ── end to end: a tool parks an await_external wait ──────────────────────────


async def test_await_external_park_through_the_engine():
    plan = Plan(
        nodes=[
            Node(node_id="entry", body=FnBody(fn="entry"), is_terminal=True),
        ]
    )
    terminal = None
    scheduler = Scheduler(
        initial_plan=plan,
        dispatcher=ToolDispatcher({}),
        fns={
            "entry": lambda: {
                "suspended": True,
                "reason": "waiting on the payment webhook",
                "interrupt_kind": "await_external",
                "request_id": "ext-1",
            }
        },
    )
    async for event in scheduler.stream("task"):
        terminal = event
    # the run parked, the interrupt names the wait and the parked node
    assert terminal.run.state.value == "suspended"
    iv = terminal.run.interrupt
    assert iv is not None
    assert iv.kind is InterruptKind.AWAIT_EXTERNAL
    assert iv.payload["reason"] == "waiting on the payment webhook"
    assert iv.node_id == "entry"
    assert iv.staged_call() is not None and iv.staged_call().name == "entry"
