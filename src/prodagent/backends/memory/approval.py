"""In-process ``ApprovalStore`` — dict-backed, single-host."""

from __future__ import annotations

import logging
import time

from prodagent.ports.approval import ApprovalDecision, ApprovalRequest

logger = logging.getLogger(__name__)


class InMemoryApprovalStore:
    """Dict-backed approval store."""

    def __init__(self) -> None:
        self._requests: dict[str, ApprovalRequest] = {}

    async def create_request(self, req: ApprovalRequest) -> None:
        self._requests.setdefault(req.request_id, req)

    async def get_request(self, request_id: str) -> ApprovalRequest | None:
        return self._requests.get(request_id)

    async def submit_decision(
        self,
        request_id: str,
        decision: ApprovalDecision,
        approver_id: str = "",
    ) -> None:
        req = self._requests.get(request_id)
        if req is None:
            req = ApprovalRequest(
                request_id=request_id,
                tool_name="",
                params={},
                confidence=0.0,
                reversibility=0.5,
                context_summary="",
            )
            self._requests[request_id] = req
        req.decision = decision
        req.approver_id = approver_id
        req.decided_at = time.time()
        logger.info(
            "Approval decision submitted: %s -> %s by %s",
            request_id,
            decision,
            approver_id,
        )
