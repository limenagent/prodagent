"""Transport — port #14: the seam a distributed message plane plugs into.

Every collaboration primitive moves work as :class:`Crossing
<prodagent.coordination.messaging.envelope.Crossing>` envelopes through an
interceptor pipeline (dedupe → contract → gate → audit) and gets a
:class:`Delivery <prodagent.coordination.messaging.envelope.Delivery>`
verdict back. Today that whole round trip is in-process; the envelope's
identity fields (``message_id`` dedupe identity, ``trace_id`` lineage,
``direction``/``kind`` routing) were designed to be wire-ready, so the seam
between "in-process pipeline" and "remote plane" is exactly one method.

A Transport implementation owns the boundary traversal: in-process it runs
the crossing through the mounted pipeline
(:class:`prodagent.coordination.messaging.transport.PipelineTransport`); a
distributed runtime sends the envelope over the wire to a remote plane that
runs the same interceptors, and reports the same :class:`Delivery` verdicts
— a duplicate is a duplicate whether suppressed locally or by the remote
dedupe store, which is the whole point of having one vocabulary.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

if TYPE_CHECKING:
    from prodagent.coordination.messaging.envelope import Crossing, Delivery

__all__ = ["Transport"]


@runtime_checkable
class Transport(Protocol):
    """One boundary direction of one primitive's message plane.

    ``send`` is the wire boundary: everything before it is caller-side
    (minting the crossing), everything after it is plane-side (interceptors,
    delivery verdict). Implementations must preserve pipeline semantics —
    in-order slots, dead-letter-once on strict rejection, duplicate
    short-circuit — because callers translate :class:`Delivery` statuses
    into control-flow decisions (a spawn dies pre-flight on a rejected
    dispatch; a chain stops on a duplicate relay).
    """

    async def send(self, crossing: Crossing[Any]) -> Delivery[Any]: ...
