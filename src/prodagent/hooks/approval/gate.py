"""Human-in-the-loop approval gate"""

from __future__ import annotations

import logging
import uuid
import weakref
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

if TYPE_CHECKING:
    from prodagent.core.types import ToolCall

from prodagent.core.exceptions import SuspendPendingApproval
from prodagent.hooks.approval.formatter import ContextAwareApprovalFormatter
from prodagent.ports.approval import ApprovalDecision, ApprovalRequest

logger = logging.getLogger(__name__)


@runtime_checkable
class ApprovalProvider(Protocol):
    async def submit_decision(
        self,
        request_id: str,
        decision: ApprovalDecision,
        approver_id: str = "",
    ) -> None: ...


class ApprovalGate:
    def __init__(
        self,
        *,
        formatter: ContextAwareApprovalFormatter | None = None,
    ) -> None:
        self._fmt = formatter or ContextAwareApprovalFormatter()
        self._pending: dict[str, ApprovalRequest] = {}
        self._deferred: dict[str, ApprovalDecision] = {}
        self._wired_registries: weakref.WeakSet[Any] = weakref.WeakSet()

    def is_wired_to(self, registry: object) -> bool:
        """True if this gate already has an APPROVAL_REQUEST checker on ``registry``."""
        return registry in self._wired_registries

    def mark_wired(self, registry: object) -> None:
        """Record that a checker for this gate has been registered on ``registry``."""
        self._wired_registries.add(registry)

    async def evaluate(
        self,
        call: ToolCall,
        *,
        run_id: str = "",
        pending_approval_id: str | None = None,
    ) -> ApprovalDecision:
        # Resume: submit_decision() populated _deferred; resumed evaluate() returns it.
        if pending_approval_id is not None:
            decision = self._deferred.pop(pending_approval_id, None)
            if decision is not None:
                logger.info(
                    "Approval resumed: request=%s -> %s (deferred decision applied)",
                    pending_approval_id,
                    decision,
                )
                return decision
            logger.warning(
                "Approval resume for request=%s but no deferred decision found; will re-request",
                pending_approval_id,
            )

        request_id = str(uuid.uuid4())
        formatted = self._fmt.format(
            call,
            old_content=call.params.get("old_content") if call.params else None,
            new_content=call.params.get("new_content") if call.params else None,
            affected_count=call.params.get("count", 0) if call.params else 0,
            environment=call.params.get("environment", "unknown") if call.params else "unknown",
        )
        req = ApprovalRequest(
            request_id=request_id,
            tool_name=call.name,
            params=call.params,
            context_summary=formatted,
            run_id=run_id,
        )
        self._pending[request_id] = req

        logger.info(
            "APPROVAL SUSPENDED [%s]: tool='%s' — awaiting submit_decision()",
            request_id,
            call.name,
        )
        raise SuspendPendingApproval(
            f"Approval deferred for '{call.name}' — awaiting submit_decision().",
            tool=call.name,
            request_id=request_id,
        )

    async def submit_decision(
        self,
        request_id: str,
        decision: ApprovalDecision,
        approver_id: str = "",
    ) -> None:
        req = self._pending.get(request_id)
        self._deferred[request_id] = decision
        if req is not None:
            req.approver_id = approver_id
        logger.info(
            "Approval decision submitted: %s -> %s by %s",
            request_id,
            decision,
            approver_id,
        )
