"""ApprovalGate lifecycle — resume leaves no residue in pending/deferred."""

from __future__ import annotations

import asyncio

import pytest

from prodagent.core.exceptions import SuspendPendingApproval
from prodagent.core.types import ToolCall
from prodagent.hooks.approval import ApprovalDecision, ApprovalGate


def _call() -> ToolCall:
    return ToolCall(name="restart_pod", params={"service": "api"})


def test_pre_submitted_decision_is_consumed_on_resume() -> None:
    gate = ApprovalGate()
    asyncio.run(gate.submit_decision("req-pre", ApprovalDecision.APPROVE))
    decision = asyncio.run(
        gate.evaluate(_call(), run_id="r1", pending_approval_id="req-pre")
    )
    assert decision == ApprovalDecision.APPROVE
    assert gate._deferred == {}


def test_resume_consumes_pending_and_deferred() -> None:
    gate = ApprovalGate()
    with pytest.raises(SuspendPendingApproval) as exc_info:
        asyncio.run(gate.evaluate(_call(), run_id="r1"))
    request_id = exc_info.value.context["request_id"]

    asyncio.run(gate.submit_decision(request_id, ApprovalDecision.APPROVE))
    assert request_id in gate._pending
    assert request_id in gate._deferred

    decision = asyncio.run(
        gate.evaluate(_call(), run_id="r1", pending_approval_id=request_id)
    )
    assert decision == ApprovalDecision.APPROVE
    assert gate._pending == {}
    assert gate._deferred == {}
