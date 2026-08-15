from __future__ import annotations

import pytest

from prodagent.core.exceptions import SuspendPendingApproval
from prodagent.core.types import ToolCall
from prodagent.guardrail.approval import ApprovalDecision, ApprovalGate


def _high_risk_call() -> ToolCall:
    return ToolCall(name="rm", params={"path": "/etc/important"})


class TestDeferMode:
    async def test_no_notifier_suspends_immediately(self):
        gate = ApprovalGate()
        with pytest.raises(SuspendPendingApproval) as exc_info:
            await gate.evaluate(_high_risk_call(), run_id="r1")
        assert exc_info.value.request_id != ""
        assert exc_info.value.tool == "rm"

    async def test_submitted_decision_resumes(self):
        gate = ApprovalGate()
        call = _high_risk_call()

        with pytest.raises(SuspendPendingApproval) as exc_info:
            await gate.evaluate(call, run_id="r1")
        req_id = exc_info.value.request_id

        await gate.submit_decision(req_id, ApprovalDecision.APPROVE, approver_id="alice")

        decision = await gate.evaluate(call, run_id="r1", pending_approval_id=req_id)
        assert decision == ApprovalDecision.APPROVE

    async def test_pre_submitted_decision_resumes(self):
        gate = ApprovalGate()
        call = _high_risk_call()

        await gate.submit_decision("req-pre", ApprovalDecision.APPROVE)

        decision = await gate.evaluate(call, run_id="r1", pending_approval_id="req-pre")
        assert decision == ApprovalDecision.APPROVE

    async def test_resume_with_unknown_request_id_re_requests(self):
        gate = ApprovalGate()
        call = _high_risk_call()

        with pytest.raises(SuspendPendingApproval) as exc_info:
            await gate.evaluate(call, run_id="r1", pending_approval_id="unknown-req")
        assert exc_info.value.request_id != "unknown-req"

    async def test_submit_decision_records_approver(self):
        gate = ApprovalGate()
        call = _high_risk_call()

        with pytest.raises(SuspendPendingApproval) as exc_info:
            await gate.evaluate(call, run_id="r1")
        req_id = exc_info.value.request_id

        await gate.submit_decision(req_id, ApprovalDecision.REJECT, approver_id="bob")

        req = gate._pending[req_id]
        assert req.approver_id == "bob"
        assert gate._deferred[req_id] == ApprovalDecision.REJECT
