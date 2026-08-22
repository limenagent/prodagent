"""Pipeline mechanics — fixed slot order, dedupe short-circuit, dead-letter
error boundary, strict vs lenient rejection, user injection, error propagation."""

from __future__ import annotations

import pytest

from prodagent.coordination.messaging.contract import MessageContract
from prodagent.coordination.messaging.envelope import (
    Crossing,
    CrossingKind,
    CrossingRejected,
    Direction,
    DuplicateCrossing,
)
from prodagent.coordination.messaging.idempotency import IdempotentMessageHandler
from prodagent.coordination.messaging.pipeline import (
    Pipeline,
    Slot,
    admission_pipeline,
    assembly_pipeline,
)
from prodagent.core.exceptions import SecurityViolation


class _RecordingDeadLetter:
    def __init__(self):
        self.calls: list[tuple[str, dict, str]] = []

    async def on_failure(self, message_id, payload, error):
        self.calls.append((message_id, payload, error))
        return "dead_letter"

    async def dead_letters(self):
        return []


class _Spy:
    """Records its slot visit; may rewrite, reject, or duplicate on demand."""

    def __init__(self, name: str, *, rewrite=None, reject=None, duplicate=False, boom=False):
        self.name = name
        self.visits: list[str] = []
        self._rewrite = rewrite
        self._reject = reject
        self._duplicate = duplicate
        self._boom = boom

    async def intercept(self, crossing):
        self.visits.append(crossing.message_id)
        if self._boom:
            raise RuntimeError(f"{self.name} bug")
        if self._duplicate:
            raise DuplicateCrossing(f"{self.name} replay")
        if self._reject is not None:
            raise self._reject
        if self._rewrite is not None:
            crossing.payload = self._rewrite(crossing.payload)
        return crossing


def _crossing(payload=None, **overrides):
    kwargs = dict(
        message_id="m-1",
        direction=Direction.UPSTREAM,
        kind=CrossingKind.RESULT,
        from_agent="child",
        to="parent",
        payload=payload if payload is not None else {"agent": "c", "output": "x", "state": "ok"},
    )
    kwargs.update(overrides)
    return Crossing(**kwargs)


# ------------------------------------------------------------- slot order


async def test_slots_run_in_fixed_order():
    seen: list[str] = []
    spies = {
        Slot.DEDUPE: _Spy("dedupe"),
        Slot.BEFORE_CONTRACT: _Spy("user-before"),
        Slot.CONTRACT: _Spy("contract"),
        Slot.AFTER_CONTRACT: _Spy("user-after"),
        Slot.GATE: _Spy("gate"),
        Slot.AUDIT: _Spy("audit"),
    }
    pipeline = Pipeline()
    for slot, spy in spies.items():
        original = spy.intercept

        async def tagged(crossing, _orig=original, _name=slot.value):
            seen.append(_name)
            return await _orig(crossing)

        spy.intercept = tagged  # type: ignore[method-assign]
        pipeline.add(slot, spy)

    delivery = await pipeline.process(_crossing())

    assert delivery.delivered
    assert seen == ["dedupe", "before_contract", "contract", "after_contract", "gate", "audit"]


async def test_interceptors_within_slot_run_in_registration_order():
    first = _Spy("first")
    second = _Spy("second")
    order: list[str] = []

    async def tag_first(c):
        order.append("first")
        return c

    async def tag_second(c):
        order.append("second")
        return c

    first.intercept = tag_first  # type: ignore[method-assign]
    second.intercept = tag_second  # type: ignore[method-assign]
    pipeline = Pipeline().add(Slot.BEFORE_CONTRACT, first).add(Slot.BEFORE_CONTRACT, second)

    assert (await pipeline.process(_crossing())).delivered
    assert order == ["first", "second"]


# --------------------------------------------------------- dedupe semantics


async def test_duplicate_short_circuits_remaining_slots_and_skips_dead_letter():
    dlq = _RecordingDeadLetter()
    dedupe = _Spy("dedupe", duplicate=True)
    later = _Spy("later")
    pipeline = Pipeline(dead_letter=dlq).add(Slot.DEDUPE, dedupe).add(Slot.GATE, later)

    delivery = await pipeline.process(_crossing())

    assert delivery.status == "duplicate"
    assert "replay" in delivery.reason
    assert later.visits == []
    assert dlq.calls == []  # a replay is not a fault


async def test_dedupe_via_handler_suppresses_second_crossing():
    handler = IdempotentMessageHandler(ttl_seconds=60.0)
    from prodagent.coordination.messaging.interceptors import DedupeInterceptor

    pipeline = Pipeline().add(Slot.DEDUPE, DedupeInterceptor(handler))
    first = await pipeline.process(_crossing(message_id="real-1"))
    second = await pipeline.process(_crossing(message_id="real-1"))

    assert first.delivered
    assert second.status == "duplicate"


# ------------------------------------------------------- rejection semantics


async def test_strict_rejection_records_dead_letter_exactly_once():
    dlq = _RecordingDeadLetter()
    rejector = _Spy(
        "contract", reject=CrossingRejected("contract violation: missing field", stage="contract")
    )
    later = _Spy("later")
    pipeline = Pipeline(dead_letter=dlq).add(Slot.CONTRACT, rejector).add(Slot.GATE, later)

    delivery = await pipeline.process(_crossing())

    assert delivery.status == "rejected"
    assert "contract violation" in delivery.reason
    assert len(dlq.calls) == 1
    assert dlq.calls[0][0] == "m-1"
    assert dlq.calls[0][1]["kind"] == "result"
    assert dlq.calls[0][1]["from_agent"] == "child"
    assert later.visits == []


async def test_lenient_rejection_records_but_original_continues():
    dlq = _RecordingDeadLetter()
    rewriter = _Spy("late", rewrite=lambda p: {**p, "rewritten": True})
    pipeline = (
        Pipeline(dead_letter=dlq)
        .add(
            Slot.CONTRACT,
            _Spy("contract", reject=CrossingRejected("off-shape", strict=False)),
        )
        .add(Slot.AFTER_CONTRACT, rewriter)
    )

    delivery = await pipeline.process(_crossing())

    assert delivery.delivered
    assert rewriter.visits == ["m-1"]  # later slots still ran
    assert delivery.crossing.payload["rewritten"] is True
    assert len(dlq.calls) == 1  # refusal on the record even though it passed


async def test_rejected_crossing_payload_unchanged_by_rejector():
    dlq = _RecordingDeadLetter()
    mutator = _Spy(
        "contract",
        reject=CrossingRejected("bad shape"),
    )
    pipeline = Pipeline(dead_letter=dlq).add(Slot.CONTRACT, mutator)
    crossing = _crossing()

    delivery = await pipeline.process(crossing)

    assert delivery.status == "rejected"
    assert delivery.crossing.payload == {"agent": "c", "output": "x", "state": "ok"}


# ------------------------------------------------------------- error policy


async def test_unexpected_exception_propagates():
    pipeline = Pipeline().add(Slot.BEFORE_CONTRACT, _Spy("buggy", boom=True))
    with pytest.raises(RuntimeError, match="buggy"):
        await pipeline.process(_crossing())


async def test_security_veto_exceptions_propagate():
    class _Vetoer:
        async def intercept(self, crossing):
            raise SecurityViolation("injected instruction detected")

    pipeline = Pipeline().add(Slot.GATE, _Vetoer())
    with pytest.raises(SecurityViolation):
        await pipeline.process(_crossing())


# ---------------------------------------------------------------- presets


async def test_admission_pipeline_rewrites_to_whitelisted_view():
    contract = MessageContract(
        required_fields=["agent", "output", "state"], optional_fields=["turns"]
    )
    pipeline = admission_pipeline(contract=contract)
    payload = {
        "agent": "c",
        "output": "result",
        "state": "ok",
        "turns": 2,
        "tool_history": [{"name": "secret_tool"}],
        "reasoning": "should not cross",
    }

    delivery = await pipeline.process(_crossing(payload=payload))

    assert delivery.delivered
    assert delivery.crossing.payload == {
        "agent": "c",
        "output": "result",
        "state": "ok",
        "turns": 2,
    }


async def test_admission_pipeline_rejects_on_contract_violation_with_dead_letter():
    dlq = _RecordingDeadLetter()
    contract = MessageContract(required_fields=["output", "state"], strict=True)
    pipeline = admission_pipeline(contract=contract, dead_letter=dlq)

    delivery = await pipeline.process(_crossing(payload={"output": "ok"}))

    assert delivery.status == "rejected"
    assert len(dlq.calls) == 1


async def test_admission_pipeline_contract_callable_may_admit_as_is():
    contract = MessageContract(required_fields=["output", "state"])

    def resolve(crossing):
        payload = crossing.payload
        return None if payload.get("agent") == "trusted" else contract

    pipeline = admission_pipeline(contract=resolve)

    trusted = await pipeline.process(_crossing(payload={"agent": "trusted", "anything": "goes"}))
    untrusted = await pipeline.process(_crossing(payload={"agent": "other"}))

    assert trusted.delivered
    assert trusted.crossing.payload == {"agent": "trusted", "anything": "goes"}
    assert untrusted.status == "rejected"


async def test_assembly_pipeline_has_no_contract_slot():
    # Even a payload that would fail any contract crosses untouched —
    # downstream containers are whitelists by construction, nothing to admit.
    pipeline = assembly_pipeline()
    delivery = await pipeline.process(_crossing(direction=Direction.DOWNSTREAM, payload="pkt"))

    assert delivery.delivered
    assert delivery.crossing.payload == "pkt"


def test_describe_lists_mounted_capabilities():
    pipeline = admission_pipeline(contract=MessageContract(required_fields=["output"]))
    text = pipeline.describe()
    assert "contract: [ContractInterceptor]" in text
    assert "gate: [GateInterceptor]" in text
