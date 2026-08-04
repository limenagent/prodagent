"""Human-in-the-loop approval hook bundle."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from prodagent.core.exceptions import SuspendPendingApproval
from prodagent.core.types import ToolCall, ToolMeta
from prodagent.guardrail.approval import ApprovalDecision, should_request_review
from prodagent.hooks.checkpoint import BlockingResult, CheckPoint
from prodagent.hooks.events import HookEvent

if TYPE_CHECKING:
    from prodagent.guardrail.approval import ApprovalGate
    from prodagent.hooks.registry import HookRegistry

logger = logging.getLogger(__name__)

_DEFAULT_REVERSIBILITY = 0.5


class ApprovalHooks:
    def __init__(
        self,
        *,
        gate: ApprovalGate | None = None,
    ) -> None:
        if gate is None:
            from prodagent.guardrail.approval.gate import ApprovalGate

            gate = ApprovalGate()
        self._gate = gate
        self._hooks: HookRegistry | None = None

    @property
    def approval_gate(self) -> ApprovalGate:
        """Public accessor — keeps Agent from reaching into ``_gate``."""
        return self._gate

    def attach(self, hooks: HookRegistry) -> None:
        self._hooks = hooks
        if self._gate.is_wired_to(hooks):
            return
        self._gate.mark_wired(hooks)
        hooks.register_checker(CheckPoint.APPROVAL_REQUEST, self.gate_request, priority=100)

    async def gate_request(
        self,
        *,
        name: str = "",
        params: dict[str, Any] | None = None,
        confidence: float | None = None,
        meta: ToolMeta | None = None,
        run_id: str = "",
        pending_approval_id: str | None = None,
        **_: Any,
    ) -> BlockingResult | None:
        if confidence is None:
            decision = ApprovalDecision.FULL_APPROVAL
            logger.info(
                "[ApprovalHooks] confidence unreported — routing HIGH tool to "
                "human approval: tool=%s",
                name,
            )
        else:
            decision = should_request_review(meta, confidence)
        if decision == ApprovalDecision.AUTO_EXECUTE:
            logger.debug(
                "[ApprovalHooks] AUTO_EXECUTE: tool=%s conf=%.2f rev=%.2f",
                name,
                confidence if confidence is not None else float("nan"),
                meta.reversibility if meta else _DEFAULT_REVERSIBILITY,
            )
            return None

        if pending_approval_id is None and self._hooks is not None:
            await self._hooks.fire(
                HookEvent.APPROVAL_REQUEST,
                name=name,
                params=params or {},
                level="HIGH",
                confidence=confidence if confidence is not None else 0.0,
                run_id=run_id,
            )

        call = ToolCall(name=name, params=params or {})
        try:
            gate_decision = await self._gate.evaluate(
                call,
                confidence=confidence if confidence is not None else 0.0,
                reversibility=(
                    (
                        meta.reversibility
                        if meta.reversibility is not None
                        else _DEFAULT_REVERSIBILITY
                    )
                    if meta
                    else _DEFAULT_REVERSIBILITY
                ),
                run_id=run_id,
                pending_approval_id=pending_approval_id,
            )
        except SuspendPendingApproval:
            # Re-raise so dispatcher persists request_id before halting.
            raise

        if gate_decision == ApprovalDecision.REJECT:
            logger.info("[ApprovalHooks] rejected: tool=%s", name)
            return BlockingResult(blocked=True, reason=f"Human rejected execution of '{name}'.")

        logger.info(
            "[ApprovalHooks] approved: tool=%s decision=%s",
            name,
            gate_decision.value,
        )
        return None
