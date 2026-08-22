"""Pipeline — the checkpoint every crossing flows through, plus two presets.

The plane's execution rules, deliberately few:

- **Slot order is fixed.** ``DEDUPE → BEFORE_CONTRACT → CONTRACT →
  AFTER_CONTRACT → GATE → AUDIT`` for every pipeline, both directions.
  Intercept-before-dedupe would burn policy on replays; gate-before-contract
  would feed the security checkpoint unvalidated shapes; audit must see what
  actually crossed, so it is last. Nobody — users, primitives — reorders.
- **Dead letter is the error boundary, not a stage.** Every strict rejection
  is recorded to the :data:`~prodagent.ports.dead_letter.DeadLetterStore`
  exactly once, here, before the primitive learns the outcome.
- **Dedupe short-circuits.** A duplicate skips every remaining interceptor and
  is *not* dead-lettered — a replay is not a fault.
- **Lenient rejections pass on.** ``CrossingRejected(strict=False)`` records
  the refusal but lets the original crossing continue (lenient contract
  semantics); later slots still run.
- **Real errors propagate.** ``SECURITY_VETO_EXCEPTIONS`` and unexpected
  exceptions are the caller's bug, not a governance outcome — they unwind.

Two presets are the entire policy surface: ``admission_pipeline`` (UPSTREAM)
and ``assembly_pipeline`` (DOWNSTREAM). ``BEFORE_CONTRACT``/``AFTER_CONTRACT``
stay open on both for user-injected semantics (injection rules, judges,
redaction) — the framework ships mechanics, apps ship policy.
"""

from __future__ import annotations

import logging
from enum import StrEnum
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

from prodagent.coordination.messaging.envelope import (
    Crossing,
    CrossingRejected,
    Delivery,
    DuplicateCrossing,
)
from prodagent.coordination.messaging.interceptors import (
    AuditInterceptor,
    ContractInterceptor,
    DedupeInterceptor,
    GateInterceptor,
    TrimInterceptor,
)

if TYPE_CHECKING:
    from collections.abc import Callable

    from prodagent.coordination.messaging.contract import MessageContract
    from prodagent.coordination.messaging.idempotency import IdempotentMessageHandler
    from prodagent.hooks.events import HookEvent
    from prodagent.hooks.registry import HookRegistry
    from prodagent.ports.dead_letter import DeadLetterStore

logger = logging.getLogger(__name__)

__all__ = [
    "Slot",
    "Interceptor",
    "Pipeline",
    "admission_pipeline",
    "assembly_pipeline",
]

_DEAD_LETTER_PREVIEW_CHARS = 2000
"""Bounded preview of an unadmitted payload recorded for post-mortem — the
dead-letter store is an operator console, not a poison-data archive."""


class Slot(StrEnum):
    """Fixed positions on the pipeline. Order is part of the contract."""

    DEDUPE = "dedupe"
    """Replay suppression — may short-circuit the whole pipeline."""

    BEFORE_CONTRACT = "before_contract"
    """User injection point ahead of structural admission."""

    CONTRACT = "contract"
    """Structural admission: validate + rewrite to the whitelisted view."""

    AFTER_CONTRACT = "after_contract"
    """User injection point after admission; built-in trim/projection lives here."""

    GATE = "gate"
    """Security veto — ``Gate.AGENT_HANDOFF``; no-op without checkers."""

    AUDIT = "audit"
    """Fire-only lifecycle events — last, so it sees what actually crossed."""


_SLOT_ORDER: tuple[Slot, ...] = (
    Slot.DEDUPE,
    Slot.BEFORE_CONTRACT,
    Slot.CONTRACT,
    Slot.AFTER_CONTRACT,
    Slot.GATE,
    Slot.AUDIT,
)


@runtime_checkable
class Interceptor(Protocol):
    """One capability on the pipeline. May rewrite the crossing in place.

    Raise :class:`~prodagent.coordination.messaging.envelope.DuplicateCrossing`
    to short-circuit as a replay, or ``CrossingRejected`` to refuse. Return
    anything else and the (possibly rewritten) crossing continues.
    """

    async def intercept(self, crossing: Crossing[Any]) -> Crossing[Any]: ...


def _dead_letter_payload(crossing: Crossing[Any]) -> dict[str, Any]:
    preview = str(crossing.payload)
    return {
        "kind": crossing.kind.value,
        "direction": crossing.direction.value,
        "from_agent": crossing.from_agent,
        "to": crossing.to,
        "payload": preview[:_DEAD_LETTER_PREVIEW_CHARS],
    }


class Pipeline:
    """One pipeline per (primitive, direction). Reused across crossings."""

    def __init__(self, *, dead_letter: DeadLetterStore | None = None) -> None:
        self._slots: dict[Slot, list[Interceptor]] = {slot: [] for slot in _SLOT_ORDER}
        self._dead_letter = dead_letter

    def add(self, slot: Slot, interceptor: Interceptor) -> Pipeline:
        """Mount a capability at a named slot. Returns ``self`` for chaining."""
        self._slots[slot].append(interceptor)
        return self

    async def process(self, crossing: Crossing[Any]) -> Delivery[Any]:
        """Run one crossing through every mounted capability, in slot order."""
        current = crossing
        for slot in _SLOT_ORDER:
            for interceptor in self._slots[slot]:
                try:
                    current = await interceptor.intercept(current)
                except DuplicateCrossing as dup:
                    return Delivery("duplicate", current, dup.reason, "dedupe")
                except CrossingRejected as rejection:
                    if self._dead_letter is not None:
                        await self._dead_letter.on_failure(
                            current.message_id,
                            _dead_letter_payload(current),
                            rejection.reason,
                        )
                    if not rejection.strict:
                        logger.warning(
                            "Lenient rejection at %s[%s]: %s — original continues",
                            slot.value,
                            current.kind.value,
                            rejection.reason,
                        )
                        continue
                    logger.warning(
                        "Crossing rejected at %s[%s]: %s",
                        slot.value,
                        current.kind.value,
                        rejection.reason,
                    )
                    return Delivery("rejected", current, rejection.reason, rejection.stage)
        return Delivery("delivered", current)

    def describe(self) -> str:
        """Human-readable mount map — debugging what a primitive wired."""
        parts = [
            f"{slot.value}: [{', '.join(type(i).__name__ for i in mounted)}]"
            for slot, mounted in self._slots.items()
            if mounted
        ]
        return " | ".join(parts) or "(empty)"


def admission_pipeline(
    *,
    contract: MessageContract | Callable[[Crossing[Any]], MessageContract | None] | None = None,
    dedupe: IdempotentMessageHandler | None = None,
    trim: Callable[[Any], Any] | None = None,
    hooks: HookRegistry | None = None,
    dead_letter: DeadLetterStore | None = None,
    audit_event: Callable[[Crossing[Any]], tuple[HookEvent, dict[str, Any]] | None] | None = None,
    max_chars: int = 2000,
) -> Pipeline:
    """UPSTREAM preset — gate what a producing agent sends into shared state.

    Mounts ``DEDUPE → CONTRACT → (trim at AFTER_CONTRACT) → GATE → AUDIT``.
    ``contract`` may resolve per crossing (e.g. a board's per-key contracts) and
    may resolve to ``None`` (admit as-is — no declared shape, no opinion).
    """
    pipeline = Pipeline(dead_letter=dead_letter)
    if dedupe is not None:
        pipeline.add(Slot.DEDUPE, DedupeInterceptor(dedupe))
    if contract is not None:
        pipeline.add(Slot.CONTRACT, ContractInterceptor(contract))
    if trim is not None:
        pipeline.add(Slot.AFTER_CONTRACT, TrimInterceptor(trim))
    pipeline.add(Slot.GATE, GateInterceptor(hooks, max_chars=max_chars))
    if audit_event is not None and hooks is not None:
        pipeline.add(Slot.AUDIT, AuditInterceptor(hooks, audit_event))
    return pipeline


def assembly_pipeline(
    *,
    dedupe: IdempotentMessageHandler | None = None,
    trim: Callable[[Any], Any] | None = None,
    hooks: HookRegistry | None = None,
    dead_letter: DeadLetterStore | None = None,
    audit_event: Callable[[Crossing[Any]], tuple[HookEvent, dict[str, Any]] | None] | None = None,
    max_chars: int = 2000,
) -> Pipeline:
    """DOWNSTREAM preset — build what a consuming agent's context receives.

    Mounts ``DEDUPE → (trim/projection at AFTER_CONTRACT) → GATE → AUDIT``.
    No contract slot: downstream containers are assembled by deterministic
    code, so the container itself is the whitelist — there is nothing to admit.
    """
    pipeline = Pipeline(dead_letter=dead_letter)
    if dedupe is not None:
        pipeline.add(Slot.DEDUPE, DedupeInterceptor(dedupe))
    if trim is not None:
        pipeline.add(Slot.AFTER_CONTRACT, TrimInterceptor(trim))
    pipeline.add(Slot.GATE, GateInterceptor(hooks, max_chars=max_chars))
    if audit_event is not None and hooks is not None:
        pipeline.add(Slot.AUDIT, AuditInterceptor(hooks, audit_event))
    return pipeline
