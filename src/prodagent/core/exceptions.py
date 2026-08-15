"""Custom exception hierarchy for agent-engineering."""

from __future__ import annotations

from typing import Any


class AgentError(Exception):
    """Root of the exception tree."""

    def __init__(self, message: str, **context: Any) -> None:
        super().__init__(message)
        self.message = message
        self.context: dict[str, Any] = context


class BudgetExceeded(AgentError):
    """Hard budget (turns / seconds / tokens / USD) exhausted."""


class InfiniteLoopDetected(AgentError):
    """Same tool-call fingerprint repeated beyond threshold."""


class ToolAbortError(AgentError):
    """Tool returned ABORT — the tool itself signalled failure."""


class ToolBlockedError(AgentError):
    """Tool returned BLOCKED — HITL approval was denied."""


class ContractViolationError(AgentError):
    """Handoff result does not conform to the declared contract."""


class PermissionDenied(AgentError):
    """Tool call not permitted for this agent or session."""


class PromptInjectionDetected(AgentError):
    """Input contains prompt-injection payload; request quarantined."""


class SensitiveContentDetected(AgentError):
    """Output contains sensitive content (PII/secret) per the app's policy; vetoed."""


class SuspendPendingApproval(AgentError):
    """Execution suspended pending human approval.

    Carries request_id so the resuming caller can correlate with submit_decision.
    """

    def __init__(
        self,
        message: str,
        *,
        tool: str = "",
        request_id: str = "",
        **context: Any,
    ) -> None:
        super().__init__(message, tool=tool, request_id=request_id, **context)
        self.tool = tool
        self.request_id = request_id


class SecurityViolation(AgentError):
    """Generic security violation."""


class VersionConflict(AgentError):
    """Optimistic-concurrency violation on the append-only event log."""


class CorruptedCheckpointError(AgentError):
    """Checkpoint JSON unparseable or fails schema validation."""


class UnknownApprovalError(AgentError):
    """Decision submitted for a non-existent approval request."""


class PlanAlreadyCompletedError(AgentError):
    """A workflow agent's preset Plan was already executed in a prior turn."""

    def __init__(self, run_id: str) -> None:
        super().__init__(
            f"workflow plan for run={run_id} already completed — "
            "preset Plan runs once; use mode='reactive' or mode='plan_first' for further turns"
        )
        self.run_id = run_id


class RunIdCollisionError(AgentError):
    """A freshly minted run_id already has a checkpoint in the store."""

    def __init__(self, run_id: str) -> None:
        super().__init__(
            f"run_id={run_id} already has a checkpoint — refusing to mint a "
            "new turn over an orphan checkpoint; resolve the existing run first"
        )
        self.run_id = run_id


class ToolCallParseError(AgentError):
    """LLM returned a tool call whose arguments were not valid JSON."""


class LLMError(AgentError):
    """LLM call failed (network, auth, rate-limit, malformed response, etc.)."""


SECURITY_VETO_EXCEPTIONS: tuple[type[BaseException], ...] = (
    PermissionDenied,
    PromptInjectionDetected,
    SensitiveContentDetected,
    SecurityViolation,
    SuspendPendingApproval,
)
