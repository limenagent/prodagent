"""ApprovalStore port — durable human-in-the-loop approval state.

Multi-replica contract: node A suspends a run pending approval, writes the
request here. The user submits a decision on any node B. Node A (or any node
that rehydrates the run from its checkpoint) reads the decision back and
resumes. No process holds a blocking wait — resumption is driven by
re-invoking the run, not by awaiting the decision in-process.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Protocol, runtime_checkable


class ApprovalDecision(StrEnum):
    """Outcome of an approval evaluation. Persisted as the store value."""

    AUTO_EXECUTE = "auto_execute"
    BRIEF_APPROVAL = "brief_approval"
    FULL_APPROVAL = "full_approval"
    REJECT = "reject"


@dataclass
class ApprovalRequest:
    """Persisted approval request — the unit of state in ``ApprovalStore``."""

    request_id: str
    tool_name: str
    params: dict[str, object]
    confidence: float
    reversibility: float  # 0.0 = irreversible, 1.0 = fully reversible
    context_summary: str
    run_id: str = ""
    created_at: float = field(default_factory=time.time)
    decision: ApprovalDecision | None = None
    decided_at: float | None = None
    approver_id: str | None = None


@runtime_checkable
class ApprovalStore(Protocol):
    """Durable store for pending approval requests and their decisions.

    Capabilities:
      BASE (required): create_request, get_request, submit_decision
    """

    async def create_request(self, req: ApprovalRequest) -> None:
        """Persist a new pending request. Idempotent on ``req.request_id``."""
        ...

    async def get_request(self, request_id: str) -> ApprovalRequest | None:
        """Return the request, or ``None`` if it does not exist.

        The returned request carries ``decision`` if a decision has been
        submitted; ``None`` otherwise.
        """
        ...

    async def submit_decision(
        self,
        request_id: str,
        decision: ApprovalDecision,
        approver_id: str = "",
    ) -> None:
        """Record the decision against the request.

        Writing the decision does not notify any in-process waiter — the
        resuming node will observe it on the next ``get_request`` call.
        Implementations should be idempotent on ``request_id``: a second
        submit for the same id overwrites the first.
        """
        ...
