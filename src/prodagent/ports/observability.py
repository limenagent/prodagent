"""Observability and governance sinks — spans, the audit log, approvals, cache.

Family home for the book's observability-and-governance socket family (span / event_log /
approval / cache, merged 2026-08): ``SpanExporter`` sinks decision
snapshots; ``EventLog`` is the append-only audit and recovery stream (the
replay plan's source of truth lives behind this seam); ``ApprovalStore``
holds durable HITL state across replicas; ``CacheStore`` is the idempotent
LLM response cache.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import TYPE_CHECKING, Protocol, runtime_checkable

from prodagent.base.determinism import now_wall

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from prodagent.base.event_log import Event
    from prodagent.base.observability import AgentSpan
    from prodagent.kernel.types import LLMResponse

# ════════════ from span.py ════════════

@runtime_checkable
class SpanExporter(Protocol):
    """Async like every other store port — an OTLP/DB-backed exporter must
    never block the event loop from inside hook dispatch."""

    async def export(self, span: AgentSpan) -> None:
        """Sink one span. Called from inside hook dispatch — must not raise
        into the bus (implementations swallow or log their own failures)."""
        ...

    async def shutdown(self) -> None: ...


# ════════════ from event_log.py ════════════

@runtime_checkable
class EventLog(Protocol):
    """Append-only event log — the second half of event-sourced recovery.

    Capabilities:
      BASE (required): append, append_events, get_events, get_after, subscribe
    """

    async def append(self, event: Event, expected_seq: int | None = None) -> int:
        """Assign a monotonic LSN, persist, return the seq.

        ``expected_seq`` enables optimistic concurrency: raise
        ``VersionConflict`` if the stored tail seq differs.
        """
        ...

    async def append_events(self, events: list[Event], expected_seq: int | None = None) -> list[int]:
        """Batch append — the group-commit entry point for write-behind
        pipelines.

        Semantics identical to calling :meth:`append` per event, in order:
        consecutive seqs assigned, returned in order. ``expected_seq`` is
        checked against the tail before the first event of each involved
        stream (batches are single-stream in every current caller; a mixed
        batch requires every involved stream's tail to match). Backends make
        one physical write out of the batch — one file open/flush, one
        transaction — that amortization is the point.
        """
        ...

    async def get_events(self, stream_id: str) -> list[Event]:
        """Events for ``stream_id`` in append order."""
        ...

    async def get_after(self, stream_id: str, since_seq: int) -> list[Event]:
        """Events for ``stream_id`` with ``seq > since_seq`` (exact tail replay)."""
        ...

    def subscribe(self, stream_id: str, since_seq: int = 0) -> AsyncIterator[Event]:
        """Tail ``stream_id``: yield every event with ``seq > since_seq``,
        then keep following live appends — an async generator, so abandoning
        the consumer stops it. Suffix law: strictly increasing, no
        duplicates, no gaps — a subscriber sees exactly what
        ``get_after(since_seq)`` would return plus what gets appended
        afterwards, so live views, projections, and recovery read the same
        truth. Cross-process tail latency is backend-poll bounded; in-process
        appends wake subscribers immediately.
        """
        ...

    async def replicate(self, events: list[Event]) -> None:
        """Absorb pre-sequenced events at their OWN seqs — the replication
        target's write path (EXTENDED capability).

        Idempotent: an event whose ``(stream_id, seq)`` is already present
        is skipped, so re-shipping after a crash heals instead of
        duplicating. Seq preservation is what makes cross-machine recovery
        work — a checkpoint cursor written against the source's seq space
        stays valid against this store, so ``get_after(cursor)`` returns the
        same suffix it would have on the source.
        """
        ...


# ════════════ from approval.py ════════════

class ApprovalDecision(StrEnum):
    """Outcome of an approval evaluation. Persisted as the store value."""

    APPROVE = "approve"
    REJECT = "reject"


@dataclass
class ApprovalRequest:
    """Persisted approval request — the unit of state in ``ApprovalStore``."""

    request_id: str
    tool_name: str
    params: dict[str, object]
    context_summary: str
    run_id: str = ""
    created_at: float = field(default_factory=now_wall)
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


# ════════════ from cache.py ════════════

@runtime_checkable
class CacheStore(Protocol):
    """Idempotent response cache for LLM complete calls."""

    async def get(self, key: str) -> LLMResponse | None:
        """Cached response for ``key`` or ``None`` — a miss is the normal
        path, never an exception."""
        ...

    async def set(self, key: str, response: LLMResponse) -> None:
        """Overwrites silently on conflict."""
        ...
