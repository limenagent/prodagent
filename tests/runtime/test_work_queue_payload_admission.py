"""WorkQueue payload admission — malformed payloads fail at construction
(whitelist at source), governance-rejected results route through the queue's
existing fail()/dead-letter path."""

from __future__ import annotations

import pytest

from prodagent.backends.memory.dead_letter import InMemoryDeadLetterQueue
from prodagent.coordination.messaging.contract import MessageContract
from prodagent.coordination.termination import MaxRounds, TerminationPolicy
from prodagent.coordination.work_queue import (
    ItemCompletedEvent,
    ItemDeadLetteredEvent,
    ItemRequeuedEvent,
    WorkItem,
    WorkQueueSpec,
    WorkResult,
    work_queue_stream,
)
from prodagent.kernel.bus import BlockingResult, Gate, HookRegistry


class _ScriptedWorker:
    """Claims one item per call and reports a scripted result."""

    def __init__(self, name: str, results: list[WorkResult]) -> None:
        self.name = name
        self._results = list(results)

    async def try_claim_and_run(self, queue, *, name) -> WorkResult | None:
        item = await queue.claim_next(self.name)
        if item is None:
            return None
        if not self._results:
            return WorkResult(item_id=item.item_id, outcome="success")
        result = self._results.pop(0)
        return WorkResult(
            item_id=item.item_id,
            outcome=result.outcome,
            error=result.error,
            cost_usd=result.cost_usd,
            tokens=result.tokens,
        )


def _spec(workers, items, **kwargs) -> WorkQueueSpec:
    return WorkQueueSpec(
        workers={w.name: w for w in workers},
        items=items,
        dead_letter=InMemoryDeadLetterQueue(max_retries=1),
        termination=TerminationPolicy(hard_cap=MaxRounds(max_rounds=10)),
        **kwargs,
    )


async def _collect(spec: WorkQueueSpec):
    events = []
    async for event in work_queue_stream(spec):
        events.append(event)
    return events


# ---------------------------------------------------- enqueue admission


def test_bad_payload_fails_at_construction():
    contract = MessageContract(required_fields=["question"], field_types={"question": str})
    items = [
        WorkItem("ok", {"question": "1+1"}),
        WorkItem("bad", {"answer": "poisoned before birth"}),
    ]

    with pytest.raises(ValueError, match="bad"):
        _spec([_ScriptedWorker("w", [])], items, payload_contract=contract)


def test_admitted_payloads_all_pass_construction():
    contract = MessageContract(required_fields=["question"], field_types={"question": str})
    items = [WorkItem("ok-1", {"question": "1+1"}), WorkItem("ok-2", {"question": "2+2"})]

    spec = _spec([_ScriptedWorker("w", [])], items, payload_contract=contract)
    assert len(spec.items) == 2


# --------------------------------------------------- task-result admission


async def test_gate_rejected_result_becomes_failure_via_fail_path():
    registry = HookRegistry()

    async def veto(**data):
        handoff = data["handoff_data"]
        if handoff["next_action"] == "complete" and "leak" in str(
            handoff["result_data"].get("error", "")
        ):
            return BlockingResult(blocked=True, reason="poisoned report")
        return BlockingResult(blocked=False)

    registry.register_checker(Gate.AGENT_HANDOFF, veto)
    liar = _ScriptedWorker(
        "liar", [WorkResult(item_id="i-1", outcome="success", error="leak: secrets")]
    )
    honest = _ScriptedWorker("honest", [WorkResult(item_id="i-2", outcome="success")])
    spec = _spec([liar, honest], [WorkItem("i-1", "q1"), WorkItem("i-2", "q2")], hooks=registry)

    events = await _collect(spec)

    # The liar's "success" was rejected by admission → synthesized failure →
    # fail() → requeue or dead letter; the honest worker's item completed.
    kinds = [type(e) for e in events]
    assert ItemCompletedEvent in kinds
    completed = [e for e in events if isinstance(e, ItemCompletedEvent)]
    assert any(e.item_id == "i-2" for e in completed)
    assert all(e.item_id != "i-1" for e in completed)
    # i-1 ended up requeued or dead-lettered — the existing error boundary.
    assert any(isinstance(e, (ItemRequeuedEvent, ItemDeadLetteredEvent)) for e in events)


async def test_worker_error_text_bounded():
    noisy = _ScriptedWorker(
        "noisy",
        [WorkResult(item_id="i-1", outcome="failure", error="x" * 10_000)],
    )
    spec = _spec([noisy], [WorkItem("i-1", "q1")])

    events = await _collect(spec)

    requeued = [e for e in events if isinstance(e, ItemRequeuedEvent)]
    dead = [e for e in events if isinstance(e, ItemDeadLetteredEvent)]
    recorded = (requeued or dead)[0]
    text = recorded.error if hasattr(recorded, "error") else ""
    assert len(text) <= 2100 or "truncated" in text or text == ""


async def test_default_queue_without_hooks_unchanged():
    worker = _ScriptedWorker("w", [WorkResult(item_id="i-1", outcome="success")])

    events = await _collect(_spec([worker], [WorkItem("i-1", "q1")]))

    assert any(isinstance(e, ItemCompletedEvent) and e.item_id == "i-1" for e in events)
