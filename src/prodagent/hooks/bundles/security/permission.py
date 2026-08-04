"""Permission + taint tracking hook bundle."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from prodagent.hooks.checkpoint import CheckPoint
from prodagent.hooks.events import HookEvent

if TYPE_CHECKING:
    from prodagent.core.types import ToolMeta
    from prodagent.guardrail.permission import (
        ContextTaintMonitor,
        PermissionCircuitBreaker,
        PermissionMatrix,
    )
    from prodagent.hooks.registry import HookRegistry
    from prodagent.tooling.registry import ToolRegistry

logger = logging.getLogger(__name__)


class PermissionHooks:
    def __init__(
        self,
        *,
        matrix: PermissionMatrix | None = None,
        circuit_breaker: PermissionCircuitBreaker | None = None,
        taint_monitor: ContextTaintMonitor | None = None,
        tool_registry: ToolRegistry | None = None,
        agent_id: str = "",
    ) -> None:
        self._matrix = matrix
        self._breaker = circuit_breaker
        self._monitor = taint_monitor
        self._registry = tool_registry
        self._agent_id = agent_id or (matrix.agent_id if matrix else "")

    def attach(self, hooks: HookRegistry) -> None:
        # Priority 100 — deny-by-default short-circuits before downstream checkers.
        if self._matrix is not None:
            hooks.register_checker(CheckPoint.TOOL_CALL, self.check_permission, priority=100)

        if self._monitor is not None:
            hooks.register_checker(CheckPoint.TOOL_CALL, self.check_taint, priority=90)
            hooks.register_checker(CheckPoint.TOOL_RESULT, self.update_taint, priority=90)
            hooks.register_event(HookEvent.SESSION_START, self.on_session_start)
            hooks.register_event(HookEvent.SESSION_END, self.on_session_end)

    def check_permission(
        self,
        *,
        name: str = "",
        params: dict[str, Any] | None = None,
        side_effect_level: str = "",
        readonly: bool = False,
        **_: Any,
    ) -> None:
        if self._matrix is None:
            return
        from prodagent.core.exceptions import SECURITY_VETO_EXCEPTIONS

        params = params or {}

        if self._breaker is not None:
            self._breaker.check(self._matrix.agent_id)

        # Repeated denials escalate to suspension, not just per-call refusal.
        operation = "read" if readonly else "execute"
        try:
            self._matrix.assert_allows(operation, name, params)
        except SECURITY_VETO_EXCEPTIONS as exc:
            self._record_violation_safe(self._matrix.agent_id, exc)
            raise

    def _should_skip(self, run: Any = None, run_id: str = "", depth: int = 0) -> bool:
        """Sub-agents share the parent monitor; inherit taint, don't reset.

        Peer continuations are top-level — they start a fresh taint session.
        Distinguishing the two relies on depth: a child sub-agent runs in its
        own orchestrator (depth=0) with a ``parent::child`` run_id, while a
        peer continuation runs in the same orchestrator at depth>=1."""
        if self._monitor is None:
            return True
        # Peer continuation (depth>=1) — fresh taint session, don't skip.
        if depth > 0:
            return False
        # depth==0 — root or child sub-agent. Child sub-agent has ``::`` in run_id.
        if run is not None:
            from prodagent.core.state.run import is_child_subordinate

            return is_child_subordinate(run)
        if run_id:
            from prodagent.core.state.run import is_child_run_id

            return is_child_run_id(run_id)
        return False

    def on_session_start(
        self, *, run_id: str = "", run: Any = None, depth: int = 0, **_: Any
    ) -> None:
        if self._should_skip(run, run_id, depth) or self._monitor is None:
            return
        # Raises if a session is already active — mid-task caller can't wipe taint.
        self._monitor.begin_session()

    def on_session_end(
        self, *, run_id: str = "", run: Any = None, depth: int = 0, **_: Any
    ) -> None:
        if self._should_skip(run, run_id, depth) or self._monitor is None:
            return
        self._monitor.end_session()

    def check_taint(self, *, name: str = "", **_: Any) -> None:
        if self._monitor is None:
            return
        from prodagent.core.exceptions import SECURITY_VETO_EXCEPTIONS

        if self._breaker and self._agent_id:
            self._breaker.check(self._agent_id)

        meta = self._get_meta(name)
        try:
            self._monitor.check_before_call(name, meta)
        except SECURITY_VETO_EXCEPTIONS as exc:
            self._record_violation_safe(self._agent_id, exc)
            raise

    def update_taint(
        self, *, name: str = "", result: dict[str, Any] | None = None, **_: Any
    ) -> None:
        if self._monitor is None:
            return
        meta = self._get_meta(name)
        self._monitor.on_tool_return(result or {}, meta)
        logger.debug("[TaintTracking] post-%s taint=%s", name, self._monitor.taint.value)

    def _record_violation_safe(self, agent_id: str, exc: BaseException) -> None:
        # Original violation always re-raised by caller; breaker failure is logged.
        if not (self._breaker and agent_id):
            return
        try:
            self._breaker.record_violation(agent_id, reason=str(exc))
        except Exception as breaker_exc:
            logger.error(
                "Circuit breaker failed to record violation for agent=%s "
                "(original violation: %s; breaker error: %s). The agent "
                "is being allowed to raise the original security exception, "
                "but the breaker count may be stale.",
                agent_id,
                exc,
                breaker_exc,
            )

    def _get_meta(self, name: str) -> ToolMeta:
        from prodagent.core.types import ToolMeta

        if self._registry is not None and name in self._registry:
            return self._registry.get_meta(name)
        return ToolMeta(name=name)
