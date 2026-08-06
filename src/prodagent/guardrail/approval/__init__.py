"""prodagent.guardrail.approval — human-in-the-loop approval gate."""

from __future__ import annotations

from prodagent.guardrail.approval.formatter import ContextAwareApprovalFormatter
from prodagent.guardrail.approval.gate import ApprovalGate, ApprovalProvider
from prodagent.guardrail.approval.routing import (
    extract_confidence,
    should_request_review,
)
from prodagent.ports.approval import ApprovalDecision, ApprovalRequest

__all__ = [
    "ApprovalGate",
    "ApprovalProvider",
    "ApprovalRequest",
    "ApprovalDecision",
    "should_request_review",
    "ContextAwareApprovalFormatter",
    "extract_confidence",
]
