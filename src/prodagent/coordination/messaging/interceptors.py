"""Built-in interceptors — deterministic mechanics, zero policy.

Each maps onto one capability column of the plane: dedupe (identity),
contract (admission), trim/projection (bounded views), gate (security veto),
audit (observability). They inspect and rewrite the crossing's typed payload
duck-typed — messaging never imports the primitives, so the plane cannot grow
per-primitive special cases. Semantic policies (injection rules, LLM judges,
redaction) are user interceptors mounted at the open slots; nothing here makes
content judgements.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Mapping
from typing import TYPE_CHECKING, Any

from prodagent.coordination.messaging.envelope import (
    Crossing,
    CrossingKind,
    CrossingRejected,
    DuplicateCrossing,
)
from prodagent.core.exceptions import SECURITY_VETO_EXCEPTIONS
from prodagent.kernel.bus import Gate

if TYPE_CHECKING:
    from collections.abc import Callable

    from prodagent.coordination.floor import FloorTurn
    from prodagent.coordination.floor_projection import FloorProjection
    from prodagent.coordination.messaging.contract import MessageContract
    from prodagent.coordination.messaging.idempotency import IdempotentMessageHandler
    from prodagent.kernel.bus import HookEvent, HookRegistry

logger = logging.getLogger(__name__)

__all__ = [
    "DedupeInterceptor",
    "ContractInterceptor",
    "TrimInterceptor",
    "ProjectionInterceptor",
    "GateInterceptor",
    "AuditInterceptor",
    "handoff_data_for",
]


class DedupeInterceptor:
    """Replay suppression — a ``message_id`` crosses a pipeline once per TTL."""

    def __init__(self, handler: IdempotentMessageHandler) -> None:
        self._handler = handler

    async def intercept(self, crossing: Crossing[Any]) -> Crossing[Any]:
        if await self._handler.is_duplicate(crossing.message_id):
            raise DuplicateCrossing(
                f"{crossing.kind.value} crossing {crossing.message_id[:8]} replayed"
            )
        return crossing


class ContractInterceptor:
    """Structural admission — validate against a contract, rewrite to whitelist.

    ``contract`` may be a :class:`MessageContract` or a callable resolving one
    per crossing (returning ``None`` = admit as-is — e.g. a board admitting
    only the keys that declared a shape). Mapping payloads are rewritten to
    the whitelisted view, so the consumer only ever sees declared fields;
    non-Mapping payloads are validated (``value_type``) but not rewritten.
    """

    def __init__(
        self,
        contract: MessageContract | Callable[[Crossing[Any]], MessageContract | None],
    ) -> None:
        self._contract = contract

    def _resolve(self, crossing: Crossing[Any]) -> MessageContract | None:
        if callable(self._contract):
            return self._contract(crossing)
        return self._contract

    async def intercept(self, crossing: Crossing[Any]) -> Crossing[Any]:
        contract = self._resolve(crossing)
        if contract is None:
            return crossing
        ok, error = contract.validate(crossing.payload)
        if not ok:
            raise CrossingRejected(
                f"contract violation: {error}",
                strict=contract.strict,
                stage="contract",
            )
        if isinstance(crossing.payload, Mapping):
            crossing.payload = contract.whitelist(crossing.payload)
        return crossing


class TrimInterceptor:
    """Bound what crosses — the primitive supplies the bounding strategy.

    The strategy returns the bounded payload (a shorter packet, a capped turn,
    a truncated board value); returning ``None`` means "cannot be bounded" and
    rejects the crossing rather than letting unbounded bytes through.
    """

    def __init__(
        self,
        trim: Callable[[Any], Any],
        *,
        reason_prefix: str = "payload",
    ) -> None:
        self._trim = trim
        self._reason_prefix = reason_prefix

    async def intercept(self, crossing: Crossing[Any]) -> Crossing[Any]:
        trimmed = self._trim(crossing.payload)
        if trimmed is None:
            raise CrossingRejected(
                f"{self._reason_prefix} could not be bounded",
                stage="trim",
            )
        crossing.payload = trimmed
        return crossing


class ProjectionInterceptor:
    """Per-viewer view of a shared transcript — the floor's trim strategy.

    Wraps a :class:`~prodagent.coordination.floor_projection.FloorProjection`
    (``PublicTextOnly`` / ``SelectiveToolExposure`` / user-supplied) and applies
    it to a transcript slice for one viewer. Speaker-verbatim rules, tool-call
    whitelists, and text caps stay inside the projection — this only routes the
    slice through the plane so views get every other mounted capability too.
    """

    def __init__(self, viewer: str, projection: FloorProjection, *, limit: int = 0) -> None:
        self._viewer = viewer
        self._projection = projection
        self._limit = limit

    async def intercept(self, crossing: Crossing[Any]) -> Crossing[Any]:
        turns: list[FloorTurn] = crossing.payload
        crossing.payload = [self._projection.project(turn, viewer=self._viewer) for turn in turns]
        return crossing


class GateInterceptor:
    """Security veto — fire the existing ``AGENT_HANDOFF`` checkpoint.

    No-op unless the registry has checkers registered for the checkpoint
    (``has_check_handlers``), so mounting a gate never changes behavior for
    apps that did not opt into a security bundle. A veto (blocked result or a
    ``SECURITY_VETO_EXCEPTIONS`` raised by a checker) rejects the crossing —
    strictly: a security refusal is never lenient.
    """

    def __init__(self, hooks: HookRegistry | None, *, max_chars: int = 2000) -> None:
        self._hooks = hooks
        self._max_chars = max_chars

    async def intercept(self, crossing: Crossing[Any]) -> Crossing[Any]:
        if self._hooks is None or not self._hooks.has_check_handlers(Gate.AGENT_HANDOFF):
            return crossing
        data = handoff_data_for(crossing, max_chars=self._max_chars)
        try:
            blocked = await self._hooks.check_blocking(Gate.AGENT_HANDOFF, handoff_data=data)
        except SECURITY_VETO_EXCEPTIONS as exc:
            raise CrossingRejected(
                f"security policy rejected {crossing.kind.value} crossing: {exc}",
                stage="gate",
            ) from exc
        if blocked.blocked:
            raise CrossingRejected(
                f"security policy blocked {crossing.kind.value} crossing",
                stage="gate",
            )
        return crossing


def handoff_data_for(crossing: Crossing[Any], *, max_chars: int = 2000) -> dict[str, Any]:
    """Deterministic ``AGENT_HANDOFF`` payload per crossing kind.

    Every shape satisfies ``validate_handoff_security`` (status / result_data /
    next_action present, no ``raw_llm_output``, canonical action vocabulary)
    so an app's configured checkers work unchanged across all five primitives.
    Payloads are read duck-typed — see the module docstring on decoupling.
    """
    kind = crossing.kind
    payload = crossing.payload
    result_data: dict[str, Any]

    if kind is CrossingKind.RESULT:
        if isinstance(payload, Mapping):
            result_data = {
                "agent": payload.get("agent", crossing.from_agent),
                "output": str(payload.get("output", ""))[:max_chars],
                "turns": payload.get("turns", 0),
            }
            status = str(payload.get("state", "unknown"))
        else:
            result_data = {"agent": crossing.from_agent}
            status = "unknown"
        return _handoff_data(status, result_data, "complete")

    if kind in (CrossingKind.DISPATCH, CrossingKind.HANDOFF):
        # Packet-like payloads (HandoffPacket) carry the task; rendered views
        # (a transcript slice, a board snapshot) get a bounded preview.
        task = getattr(payload, "task_description", None)
        if task is not None:
            result_data = {
                "to": crossing.to,
                "task": str(task)[:max_chars],
            }
            prior = getattr(payload, "prior_output", "")
            if prior:
                result_data["prior_output"] = str(prior)[:max_chars]
        else:
            result_data = {"to": crossing.to, "preview": str(payload)[:max_chars]}
        return _handoff_data("dispatched", result_data, "delegate")

    if kind is CrossingKind.SPEECH:
        result_data = {
            "speaker": getattr(payload, "speaker", crossing.from_agent),
            "text": str(getattr(payload, "text", ""))[:max_chars],
            "round": getattr(payload, "round", 0),
        }
        return _handoff_data("spoken", result_data, "speak")

    if kind is CrossingKind.WRITE:
        # The crossing payload is the written value; key and author ride the
        # envelope (to / from_agent).
        result_data = {
            "author": crossing.from_agent,
            "key": crossing.to,
            "value": str(payload)[:max_chars],
        }
        return _handoff_data("written", result_data, "write")

    if kind is CrossingKind.TASK_RESULT:
        result_data = {
            "worker": crossing.from_agent,
            "item_id": getattr(payload, "item_id", crossing.to),
            "error": str(getattr(payload, "error", "") or "")[:max_chars],
        }
        return _handoff_data(str(getattr(payload, "outcome", "unknown")), result_data, "complete")

    # CrossingKind.ENQUEUE
    result_data = {
        "item_id": getattr(payload, "item_id", crossing.to),
        "preview": str(getattr(payload, "payload", ""))[:max_chars],
    }
    return _handoff_data("enqueued", result_data, "retry")


def _handoff_data(status: str, result_data: dict[str, Any], next_action: str) -> dict[str, Any]:
    return {"status": status, "result_data": result_data, "next_action": next_action}


class AuditInterceptor:
    """Fire-only lifecycle observability — last slot, sees what crossed.

    The primitive supplies the mapping (crossing → ``(HookEvent, fields)`` or
    ``None`` to stay silent); only existing hook events are emitted — the
    plane mints no vocabulary of its own.
    """

    def __init__(
        self,
        hooks: HookRegistry,
        event_for: Callable[[Crossing[Any]], tuple[HookEvent, dict[str, Any]] | None],
    ) -> None:
        self._hooks = hooks
        self._event_for = event_for

    async def intercept(self, crossing: Crossing[Any]) -> Crossing[Any]:
        mapped = self._event_for(crossing)
        if mapped is not None:
            event, fields = mapped
            await self._hooks.fire(event, **fields)
        return crossing
