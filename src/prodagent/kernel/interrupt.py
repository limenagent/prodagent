"""Interrupt — the run's vocabulary for "stopped, waiting on the world"
(column 20).

A true pause lets go: the process can die and the run resumes elsewhere.
What makes that safe is what the park stores — the run's state (the
résumé), the *frozen action* that was about to fire (so resume executes
exactly that call, parameters verbatim — never a re-ask of the model), and
the Interrupt itself: a serializable "what are we waiting for" that rides
the checkpoint across processes.

Three trigger kinds cover every wait there is — the model needs a fact
from the human (``need_input``), a side effect needs the human's blessing
(``approve``), a long task waits on an external event (``await_external``)
— and a new reason is a new payload, not a new mechanism. Cancellation is
the other thing entirely: "not wanted anymore" ends the run; an interrupt
is "still wanted, later" — SUSPENDED → RUNNING is the only way back.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from prodagent.kernel.types import ToolCall

__all__ = ["InterruptKind", "Interrupt", "PendingAction"]


class InterruptKind(StrEnum):
    """Why a run let go — three trigger positions, one mechanism."""

    NEED_INPUT = "need_input"
    """The model is missing a fact only the human has; the answer becomes
    the resumed input."""
    APPROVE = "approve"
    """A side effect is staged and needs the human's blessing before it
    fires (the HITL gate)."""
    AWAIT_EXTERNAL = "await_external"
    """The run waits on an event the process cannot produce — a callback,
    a file landing, another system's answer."""


@dataclass(frozen=True, slots=True)
class Interrupt:
    """What a suspended run is waiting on — serializable, checkpoint-bound.

    ``payload`` says what "waiting" means for this kind (the staged
    action's summary, the question asked, the external event's identity).
    A new kind of wait is a new payload shape, never a kernel change."""

    kind: InterruptKind
    request_id: str = ""
    payload: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {"kind": self.kind.value, "request_id": self.request_id, "payload": self.payload}

    @classmethod
    def from_dict(cls, d: dict[str, Any] | None) -> Interrupt | None:
        if not d:
            return None
        kind = d.get("kind", InterruptKind.APPROVE.value)
        return cls(
            kind=InterruptKind(kind),
            request_id=str(d.get("request_id", "")),
            payload=dict(d.get("payload") or {}),
        )


@dataclass(frozen=True, slots=True)
class PendingAction:
    """The frozen action a park carries: the exact call that was about to
    fire, paired with the interrupt that stopped it.

    This is the "resume executes the original one" guarantee as data: the
    approved object and the executed object are the same object — resume
    replays ``action`` verbatim (idempotency keys and all), it never asks
    the model again what it meant to do."""

    action: ToolCall
    interrupt: Interrupt
