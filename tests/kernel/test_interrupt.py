"""Interrupt — the park's vocabulary and the door table (column 20/8).

The laws under test: a park stores three things (state, the frozen action,
the Interrupt); the kind is payload, not mechanism (need_input / approve /
await_external ride the same door); the wire round-trips the kind so a
resumed process knows what it was waiting for; old checkpoints read back
as the historical kind; and the four doors enforce the allowed-transition
table — the nonsensical endings (dead end into success, suspended straight
into failed) are illegal at the write site.
"""

from __future__ import annotations

import pytest

from prodagent.base.errors import IllegalTransition
from prodagent.kernel.bodies import FnBody
from prodagent.kernel.graph import Node, compile_planned
from prodagent.kernel.interrupt import Interrupt, InterruptKind, PendingAction
from prodagent.kernel.run import Run
from prodagent.kernel.scheduler import Scheduler
from prodagent.kernel.types import RunState, ToolCall
from prodagent.tooling.dispatcher import ToolDispatcher


def _call(name: str = "do_thing") -> ToolCall:
    return ToolCall(name=name, params={"x": 1})


# ── the vocabulary ────────────────────────────────────────────────────────────


def test_three_trigger_kinds_one_mechanism():
    assert [k.value for k in InterruptKind] == ["need_input", "approve", "await_external"]


def test_interrupt_wire_roundtrip_carries_kind_and_payload():
    it = Interrupt(InterruptKind.AWAIT_EXTERNAL, "req-1", {"event": "callback"})
    assert Interrupt.from_dict(it.to_dict()) == it


def test_pending_action_pairs_the_frozen_call_with_its_interrupt():
    action = PendingAction(_call(), Interrupt(InterruptKind.APPROVE, "req-2"))
    assert action.action.name == "do_thing"
    assert action.interrupt.kind is InterruptKind.APPROVE


# ── the park and its views ────────────────────────────────────────────────────


def _parked_run(**kwargs) -> Run:
    run = Run(run_id="r", task="t")
    assert run.park_for_approval(_call(), "req-9", **kwargs)
    return run


def test_default_park_reads_back_as_approve():
    run = _parked_run()
    it = run.interrupt()
    assert it is not None and it.kind is InterruptKind.APPROVE
    assert it.request_id == "req-9"


def test_another_kind_of_wait_is_just_payload():
    run = _parked_run(
        interrupt=Interrupt(InterruptKind.NEED_INPUT, "req-3", {"question": "which account?"})
    )
    it = run.interrupt()
    assert it is not None and it.kind is InterruptKind.NEED_INPUT
    assert it.payload["question"] == "which account?"


def test_old_checkpoints_read_back_as_the_historical_kind():
    run = _parked_run()
    run.pending_interrupt = None  # strip the wire field a legacy dict lacks
    it = run.interrupt()
    assert it is not None and it.kind is InterruptKind.APPROVE


def test_a_handoff_park_is_a_transfer_not_an_interrupt():
    run = _parked_run()
    assert run.interrupt() is not None
    run.pending_tool_call = None
    assert run.interrupt() is None


def test_clearing_the_park_clears_the_interrupt():
    run = _parked_run(
        interrupt=Interrupt(InterruptKind.AWAIT_EXTERNAL, "req-4", {"event": "webhook"})
    )
    park = run.clear_approval_park()
    assert park is not None and park.request_id == "req-9"
    assert run.interrupt() is None
    assert run.pending_interrupt is None


def test_park_wire_roundtrip_keeps_the_kind():
    from prodagent.kernel.run import Run as RunCls

    run = _parked_run(interrupt=Interrupt(InterruptKind.NEED_INPUT, "req-5", {"q": "how many?"}))
    restored = RunCls.from_dict(run.to_dict())
    it = restored.interrupt()
    assert it is not None and it.kind is InterruptKind.NEED_INPUT
    assert it.payload == {"q": "how many?"}


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


def test_suspended_ends_by_handoff_door():
    from prodagent.kernel.run import PendingHandoff

    run = Run(run_id="r4", task="t")
    run.suspend("waiting on approval")
    assert run.park_handoff(PendingHandoff(peer_name="peer", task="carry on"))
    assert run.state is RunState.COMPLETED


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
    plan = compile_planned(
        [
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
    # the run parked, the frozen action carries the external wait
    assert terminal.run.state.value == "suspended"
    park = terminal.run.resume_point()
    assert park is not None and park.call.name == "entry"
    it = terminal.run.interrupt()
    assert it is not None
    assert it.kind is InterruptKind.AWAIT_EXTERNAL
    assert it.payload["reason"] == "waiting on the payment webhook"
