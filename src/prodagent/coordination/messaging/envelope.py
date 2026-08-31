"""Crossing — the one envelope every agent-boundary message travels in.

Collaboration primitives (``agents=`` tree, ``peers=`` chain, Blackboard board) share one fact: collaborating means
messages crossing agent boundaries. A ``Crossing`` is one such boundary
traversal, wrapped in a uniform envelope so every crossing — regardless of
which primitive produced it — flows through the same checkpoint and gets the
same capabilities (dedupe, contract admission, projection, security gate,
audit) via :mod:`prodagent.coordination.messaging.pipeline`.

Direction is the primary axis — exactly two semantics, no per-primitive
variants:

- ``DOWNSTREAM`` (assembly): task/state flowing *toward* a consuming agent's
  context. The container is assembled by deterministic code, so sanitization
  happens at the source — the container itself is the whitelist.
- ``UPSTREAM`` (admission): a result/speech/write *produced by* an agent
  entering shared state or another agent's context. The payload is LLM-freely
  generated, so a gatekeeper at the destination decides whether it crosses.

The envelope never flattens the payload: a ``Crossing`` carries the primitive's
typed object (``HandoffPacket``, ``FloorTurn``, ``BoardWrite``, ...) verbatim.
``kind`` is an observability tag (it selects e.g. the guard-gate payload
shape), not a behavior switch — behavior is decided by ``direction`` and by
which interceptors the primitive mounted on its pipeline.
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Generic, TypeVar

__all__ = [
    "Direction",
    "CrossingKind",
    "Crossing",
    "CrossingStopped",
    "DuplicateCrossing",
    "CrossingRejected",
    "Delivery",
]

T = TypeVar("T")


class Direction(StrEnum):
    """Which way the boundary is being crossed — the plane's primary axis."""

    DOWNSTREAM = "downstream"
    """Toward a consuming agent's context: assemble/whitelist at the source."""

    UPSTREAM = "upstream"
    """Away from a producing agent into shared state: admit at the destination."""


class CrossingKind(StrEnum):
    """Observability tag naming *what* is crossing, not how to treat it.

    Treatment is decided by :class:`Direction` + mounted interceptors; the kind
    only picks deterministic shapes (e.g. the ``AGENT_HANDOFF`` gate payload).
    """

    DISPATCH = "dispatch"
    """DOWNSTREAM: task packet to a child / rendered view to a member."""

    HANDOFF = "handoff"
    """DOWNSTREAM: peer relay — control transfer to the next agent in a chain."""

    RESULT = "result"
    """UPSTREAM: child result back to parent / chain root settling its output."""

    SPEECH = "speech"
    """UPSTREAM: an ensemble member's turn entering the shared floor."""

    WRITE = "write"
    """UPSTREAM: an expert's contribution entering a board slot."""

    ENQUEUE = "enqueue"
    """UPSTREAM: a producer depositing a work item into the shared queue."""

    TASK_RESULT = "task_result"
    """UPSTREAM: a worker reporting a work item outcome to the queue."""


@dataclass(slots=True)
class Crossing(Generic[T]):
    """One boundary traversal: identity + lineage + a typed payload.

    ``payload`` is the primitive's own typed object, carried verbatim — never
    flattened into a dict. Interceptors may rewrite ``payload`` (whitelist,
    trim, projection); identity fields stay immutable-in-spirit and anchor
    dedupe, dead-letter records, and trace correlation.
    """

    message_id: str
    """Dedupe identity. Unique per logical traversal; uuid4 unless supplied."""

    direction: Direction
    kind: CrossingKind
    from_agent: str
    """Producing side — an agent name."""

    to: str
    """Consuming side — agent name, board key, queue run id, or floor session."""

    payload: T
    """The typed primitive payload (HandoffPacket / FloorTurn / BoardWrite / ...)."""

    trace_id: str = ""
    """Run lineage — one collaboration stays one trace across all topologies."""

    meta: dict[str, Any] = field(default_factory=dict)
    """Free metadata (depth, run ids, trigger name, round) for gates and audit."""

    created_at: float = field(default_factory=time.monotonic)

    @classmethod
    def mint(
        cls,
        *,
        direction: Direction,
        kind: CrossingKind,
        from_agent: str,
        to: str,
        payload: T,
        trace_id: str = "",
        message_id: str = "",
        **meta: Any,
    ) -> Crossing[T]:
        """Build a crossing with a fresh identity unless one is supplied.

        Supplying ``message_id`` links traversals that belong together — e.g. a
        spawn's dispatch and its result share one id (one logical crossing,
        two directions), and a peer relay reuses the id minted when the handoff
        tool fired so crash-replays can be suppressed.
        """
        return cls(
            message_id=message_id or str(uuid.uuid4()),
            direction=direction,
            kind=kind,
            from_agent=from_agent,
            to=to,
            payload=payload,
            trace_id=trace_id,
            meta=dict(meta),
        )


class CrossingStopped(Exception):
    """Base for pipeline control flow — never surfaced to callers as an error.

    :meth:`Pipeline.process <prodagent.coordination.messaging.pipeline.Pipeline.process>`
    catches these and reports the outcome as a :class:`Delivery`.
    """


class DuplicateCrossing(CrossingStopped):
    """Dedupe hit — the remaining interceptors are skipped, nothing is dead-lettered."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class CrossingRejected(CrossingStopped):
    """An interceptor refused to let the crossing continue.

    ``strict=False`` records the refusal to the dead-letter store but lets the
    *original* (pre-interceptor) crossing continue — lenient contract
    semantics. ``strict=True`` (default) stops the crossing.
    """

    def __init__(self, reason: str, *, strict: bool = True, stage: str = "") -> None:
        super().__init__(reason)
        self.reason = reason
        self.strict = strict
        self.stage = stage


@dataclass(frozen=True, slots=True)
class Delivery(Generic[T]):
    """The fate of a crossing — the only way ``Pipeline.process`` reports it.

    ``crossing`` is the working copy: rewritten (whitelisted/trimmed) when
    delivered, untouched otherwise. ``reason`` explains non-delivery;
    ``stage`` names the slot that refused it ("contract", "gate", ...).
    """

    status: str  # Literal["delivered", "duplicate", "rejected"] — str for JSON ease
    crossing: Crossing[T]
    reason: str = ""
    stage: str = ""

    @property
    def delivered(self) -> bool:
        return self.status == "delivered"
