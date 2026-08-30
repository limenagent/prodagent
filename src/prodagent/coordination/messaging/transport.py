"""In-process Transport — the default implementation of port #14.

:class:`prodagent.ports.messaging.Transport` is the seam; this module is
what the seam points at today: a crossing ``send`` runs through a mounted
interceptor pipeline (:mod:`prodagent.coordination.messaging.pipeline`) and
comes back as a :class:`~prodagent.coordination.messaging.envelope.Delivery`.
A distributed runtime supplies its own implementation (same port, envelope
over the wire, interceptors on the remote plane) — callers must not be able
to tell the difference, which is why every preset's construction lives here
too: spawn's dispatch/result pair and the peer relay's handoff all build
their transports through :func:`build_transport`, so dedupe TTL policy and
preset selection cannot drift between primitives.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from prodagent.coordination.messaging.envelope import Crossing, Delivery, Direction
from prodagent.coordination.messaging.idempotency import IdempotentMessageHandler
from prodagent.coordination.messaging.pipeline import (
    Pipeline,
    admission_pipeline,
    assembly_pipeline,
)

if TYPE_CHECKING:
    from collections.abc import Callable

    from prodagent.coordination.messaging.contract import MessageContract
    from prodagent.kernel.bus import HookEvent, HookRegistry
    from prodagent.ports.messaging import DeadLetterStore

__all__ = ["TransportSpec", "PipelineTransport", "build_transport"]


@dataclass
class TransportSpec:
    """What a primitive declares about one boundary direction.

    ``direction`` selects the preset (DOWNSTREAM assembles at the source,
    UPSTREAM admits at the destination); ``dedupe_ttl_s`` mounts replay
    suppression with that TTL (``None`` = no dedupe); the rest are the
    preset's optional interceptors, passed through verbatim.
    """

    direction: Direction
    dead_letter: DeadLetterStore | None = None
    dedupe_ttl_s: float | None = None
    contract: MessageContract | Callable[[Crossing[Any]], MessageContract | None] | None = None
    trim: Callable[[Any], Any] | None = None
    audit_event: Callable[[Crossing[Any]], tuple[HookEvent, dict[str, Any]] | None] | None = None
    hooks: HookRegistry | None = None
    max_chars: int = 2000


class PipelineTransport:
    """Crossings run the mounted pipeline in-process — the reference
    semantics for the Transport port."""

    def __init__(self, pipeline: Pipeline) -> None:
        self._pipeline = pipeline

    @property
    def pipeline(self) -> Pipeline:
        """The mounted pipeline — for tests and ``describe()`` debugging."""
        return self._pipeline

    async def send(self, crossing: Crossing[Any]) -> Delivery[Any]:
        return await self._pipeline.process(crossing)


def build_transport(spec: TransportSpec) -> PipelineTransport:
    """Build the in-process transport for one boundary direction.

    The single home for preset selection and dedupe policy: every primitive
    that moves crossings constructs its transports here, so the fixed slot
    order, the dead-letter boundary, and the replay-suppression TTL all live
    once instead of being re-derived per call site.
    """
    dedupe = (
        IdempotentMessageHandler(ttl_seconds=spec.dedupe_ttl_s)
        if spec.dedupe_ttl_s is not None
        else None
    )
    pipeline = (
        assembly_pipeline(
            dedupe=dedupe,
            hooks=spec.hooks,
            dead_letter=spec.dead_letter,
            audit_event=spec.audit_event,
            max_chars=spec.max_chars,
        )
        if spec.direction is Direction.DOWNSTREAM
        else admission_pipeline(
            contract=spec.contract,
            dedupe=dedupe,
            trim=spec.trim,
            hooks=spec.hooks,
            dead_letter=spec.dead_letter,
            audit_event=spec.audit_event,
            max_chars=spec.max_chars,
        )
    )
    return PipelineTransport(pipeline)
