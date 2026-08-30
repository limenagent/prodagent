"""Human-in-the-loop approval gate"""

from __future__ import annotations

import logging
import uuid
import weakref
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

if TYPE_CHECKING:
    from prodagent.kernel.types import ToolCall

from prodagent.base.errors import SuspendPendingApproval
from prodagent.hooks.approval.formatter import ContextAwareApprovalFormatter
from prodagent.ports.observability import ApprovalDecision, ApprovalRequest, ApprovalStore

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
    """HITL gate with suspend/resume semantics — never a blocking wait.

    ``evaluate()`` either returns a decision that already arrived (deferred
    in-process, or read back from the durable store on another node) or
    raises ``SuspendPendingApproval``; the run parks and resumes when
    ``submit_decision()`` lands out-of-band. A human's latency is measured
    in minutes — no event loop should hold still for it."""

    def __init__(
        self,
        *,
        formatter: ContextAwareApprovalFormatter | None = None,
        store: ApprovalStore | None = None,
    ) -> None:
        self._fmt = formatter or ContextAwareApprovalFormatter()
        self._pending: dict[str, ApprovalRequest] = {}
        self._deferred: dict[str, ApprovalDecision] = {}
        # Weak on purpose: the gate outlives many short-lived forked agents —
        # a strong set would pin every registry (and its hooks) in memory.
        self._wired_registries: weakref.WeakSet[Any] = weakref.WeakSet()
        # Optional durable backing: with a store, a decision submitted on one
        # node resumes a gate rebuilt on another.
        self._store = store

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
        """Return the decision if it already exists; otherwise mint the
        request, persist it, and raise ``SuspendPendingApproval`` — the
        parking path. Resume order: in-process deferred → durable store →
        re-request (a lost decision re-prompts rather than guesses)."""
        # Resume: submit_decision() populated _deferred; resumed evaluate() returns it.
        if pending_approval_id is not None:
            decision = self._deferred.pop(pending_approval_id, None)
            if decision is not None:
                self._pending.pop(pending_approval_id, None)
                logger.info(
                    "Approval resumed: request=%s -> %s (deferred decision applied)",
                    pending_approval_id,
                    decision,
                )
                return decision
            if self._store is not None:
                stored = await self._store.get_request(pending_approval_id)
                if stored is not None and stored.decision is not None:
                    logger.info(
                        "Approval resumed: request=%s -> %s (durable store hit)",
                        pending_approval_id,
                        stored.decision,
                    )
                    return stored.decision
            logger.warning(
                "Approval resume for request=%s but no deferred decision found; will re-request",
                pending_approval_id,
            )

        request_id = str(uuid.uuid4())  # fresh identity for a fresh ask — never reuse a parked one
        # The formatter renders tool-specific context (diffs, affected counts)
        # so the human approves *this* change, not a generic tool name.
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
        if self._store is not None:
            await self._store.create_request(req)

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
        """Land a decision out-of-band: deferred for an in-process resume,
        persisted for a cross-node one. No waiter is signalled — resumption
        is driven by re-invoking the run, not by waking this process."""
        # Unknown ids are allowed: a decision may be pre-submitted for a
        # request minted before a crash (id restored from checkpoint).
        req = self._pending.get(request_id)
        self._deferred[request_id] = decision
        if req is not None:
            req.approver_id = approver_id
        if self._store is not None:
            await self._store.submit_decision(request_id, decision, approver_id=approver_id)
        logger.info(
            "Approval decision submitted: %s -> %s by %s",
            request_id,
            decision,
            approver_id,
        )
