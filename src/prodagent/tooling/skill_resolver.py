from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from prodagent.core.exceptions import SuspendPendingApproval
from prodagent.core.types import SKILL_INJECTION_KEY, ToolCall, ToolOutcome, ToolResult
from prodagent.guardrail.approval.matrix import extract_confidence
from prodagent.hooks.checkpoint import CheckPoint
from prodagent.hooks.events import HookEvent

if TYPE_CHECKING:
    from prodagent.core.types import ToolMeta
    from prodagent.evaluation.skills.registry import SkillRegistry
    from prodagent.hooks.registry import HookRegistry

logger = logging.getLogger(__name__)

__all__ = ["SkillResolver"]


class SkillResolver:
    """Resolves ``get_skill`` calls and tracks state the dispatcher needs.

    Holds the mutable state that used to live on ``ToolDispatcher``:
    the per-run cache of already-injected skill docs, and the pending
    approval id replayed on HITL resume. The dispatcher delegates
    ``get_skill`` calls here so its own ``dispatch`` path stays uniform
    (pre/post hooks, breaker, timeout) instead of special-casing skills.
    """

    __slots__ = ("_skills", "_hooks", "_agent_id", "_invoked", "_pending_approval_id")

    def __init__(
        self,
        skills: SkillRegistry | None,
        hooks: HookRegistry | None,
        *,
        agent_id: str = "",
        pending_approval_id: str | None = None,
    ) -> None:
        self._skills = skills
        self._hooks = hooks
        self._agent_id = agent_id
        self._invoked: dict[str, str] = {}
        self._pending_approval_id = pending_approval_id

    def set_pending_approval_id(self, approval_id: str | None) -> None:
        """Set the approval id to replay on the next approval gate (HITL resume)."""
        self._pending_approval_id = approval_id

    def invoked_skills(self) -> dict[str, str]:
        return dict(self._invoked)

    async def resolve(self, call: ToolCall, run_id: str) -> ToolResult:
        """Resolve a ``get_skill`` call.

        Called from the dispatcher's normal pipeline (after pre-hooks, under
        the breaker probe), so skill loads observe the same hooks and circuit
        state as any other tool. Returns a ``ToolResult`` whose ``value``
        carries ``SKILL_INJECTION_KEY`` when the skill was found.
        """
        skill_name = call.params.get("name", "")
        if self._skills is None:
            return ToolResult(
                ToolOutcome.OK,
                value={"skill": skill_name, "loaded": False},
                tool=call.name,
            )

        skill_content = self._skills.get(skill_name)
        found = skill_content is not None
        injection = self._skills.get_full_doc(skill_name) if found else ""

        if self._hooks:
            skill_path = str(p) if (p := self._skills.path_for(skill_name)) else ""
            await self._hooks.fire(
                HookEvent.SKILL_LOAD,
                name=skill_name,
                found=found,
                chars=len(injection) if injection else 0,
                path=skill_path,
                run_id=run_id,
            )

        if found:
            self._invoked[skill_name] = injection

        value: dict[str, Any] = {"skill": skill_name, "loaded": found}
        if found:
            value[SKILL_INJECTION_KEY] = injection
        return ToolResult(ToolOutcome.OK, value=value, tool=call.name)

    async def gate_approval(self, call: ToolCall, meta: ToolMeta) -> ToolResult | None:
        """Run the approval checkpoint for a HIGH side-effect tool.

        Returns ``None`` if approved, otherwise a SUSPENDED/BLOCKED result.
        """
        if not self._hooks or not self._hooks.has_check_handlers(CheckPoint.APPROVAL_REQUEST):
            return ToolResult.suspended(
                reason=f"Tool '{call.name}' requires human approval before execution",
                tool=call.name,
            )

        confidence = extract_confidence(call)
        run_id = self._agent_id
        try:
            approval = await self._hooks.check_blocking(
                CheckPoint.APPROVAL_REQUEST,
                name=call.name,
                params=call.params,
                side_effect_level=meta.side_effect_level.value,
                confidence=confidence,
                meta=meta,
                run_id=run_id,
                pending_approval_id=self._pending_approval_id,
            )
        except SuspendPendingApproval as exc:
            return ToolResult.suspended(
                reason=str(exc),
                tool=call.name,
                approval_request_id=exc.request_id,
            )
        self._pending_approval_id = None
        if approval.blocked:
            return ToolResult.blocked_by(approval.reason or "approval denied", tool=call.name)
        return None
