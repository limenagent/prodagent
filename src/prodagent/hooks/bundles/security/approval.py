"""Human-in-the-loop approval hook bundle."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from prodagent.base.errors import SuspendPendingApproval
from prodagent.hooks.approval import ApprovalDecision, ApprovalProvider
from prodagent.kernel.bus import BlockingResult, Gate, HookEvent
from prodagent.kernel.types import ToolCall

if TYPE_CHECKING:
    from prodagent.hooks.approval import ApprovalGate
    from prodagent.kernel.bus import HookRegistry

logger = logging.getLogger(__name__)


class ApprovalHooks:
    """The approval cartridge: provide the gate on the bus's typed slot and
    mount it as the APPROVAL_REQUEST checker — HIGH side-effect tools
    suspend through here (fail-closed when no approver answers)."""

    def __init__(
        self,
        *,
        gate: ApprovalGate | None = None,
    ) -> None:
        if gate is None:
            from prodagent.hooks.approval.gate import ApprovalGate

            gate = ApprovalGate()
        self._gate = gate
        self._hooks: HookRegistry | None = None

    @property
    def approval_gate(self) -> ApprovalGate:
        """Public accessor — keeps Agent from reaching into ``_gate``."""
        return self._gate

    def attach(self, hooks: HookRegistry) -> None:
        """Provide the gate, then register the checker exactly once per
        registry (re-attaching a shared gate must not double-veto)."""
        self._hooks = hooks
        hooks.provide(ApprovalProvider, self._gate)
        if self._gate.is_wired_to(hooks):
            return
        self._gate.mark_wired(hooks)
        hooks.register_checker(Gate.APPROVAL_REQUEST, self.gate_request, priority=100)

    async def gate_request(
        self,
        *,
        name: str = "",
        params: dict[str, Any] | None = None,
        run_id: str = "",
        pending_approval_id: str | None = None,
        **_: Any,
    ) -> BlockingResult | None:
        if pending_approval_id is None and self._hooks is not None:
            await self._hooks.fire(
                HookEvent.APPROVAL_REQUEST,
                name=name,
                params=params or {},
                level="HIGH",
                run_id=run_id,
            )

        call = ToolCall(name=name, params=params or {})
        try:
            gate_decision = await self._gate.evaluate(
                call,
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
