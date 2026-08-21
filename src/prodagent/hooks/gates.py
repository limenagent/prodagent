"""Blocking gates and injection points for the tri-protocol hook bus.

``Gate`` answers one question: *can execution proceed past this point?* A
checker returns a veto and the run stops there. The word "checkpoint" is
deliberately reserved for run-state persistence (``CheckpointStore``,
``run.checkpoint_version``) — the two concepts share no machinery.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


@dataclass
class BlockingResult:
    blocked: bool = False
    reason: str | None = None


class FailurePolicy(StrEnum):
    FAIL_OPEN = "fail_open"
    FAIL_CLOSED = "fail_closed"


class Gate(StrEnum):
    TOOL_CALL = "checkpoint.tool_call"
    PLAN_APPROVAL = "checkpoint.plan_approval"

    # L1-L5 security pipeline gates
    SESSION_START = "checkpoint.session_start"
    CONTEXT_BUILD = "checkpoint.context_build"
    TOOL_RESULT = "checkpoint.tool_result"
    RUN_COMPLETE = "checkpoint.run_complete"

    APPROVAL_REQUEST = "checkpoint.approval_request"
    AGENT_HANDOFF = "checkpoint.agent_handoff"
    DOCUMENT_ADD = "checkpoint.document_add"


class InjectionPoint(StrEnum):
    CONTEXT_INJECTOR = "inject.context"
