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

__all__ = ["InterruptKind", "Interrupt"]


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

    This is the ONE durable park fact: the run waits on ``kind`` for
    ``request_id``, at node ``node_id``, and ``payload`` says what waiting
    means for this kind. For an approval the staged action rides the
    payload (``staged_call``) so the resume retries that exact call —
    verbatim, never a re-ask of the model. A new kind of wait is a new
    payload shape, never a kernel change."""

    kind: InterruptKind
    request_id: str = ""
    payload: dict[str, Any] = field(default_factory=dict)
    node_id: str = ""
    """Where the run was parked — the structural half of "waiting" (the
    resume re-enters this node). Keyword argument, never positional."""

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind.value,
            "request_id": self.request_id,
            "node_id": self.node_id,
            "payload": self.payload,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any] | None) -> Interrupt | None:
        if not d:
            return None
        kind = d.get("kind", InterruptKind.APPROVE.value)
        return cls(
            kind=InterruptKind(kind),
            request_id=str(d.get("request_id", "")),
            payload=dict(d.get("payload") or {}),
            node_id=str(d.get("node_id", "")),
        )

    def staged_call(self) -> ToolCall | None:
        """The frozen action an approval parked (``None`` for waits that
        stage no call — plan review, questions, external events)."""
        from prodagent.kernel.types import ToolCall

        staged = self.payload.get("call")
        if isinstance(staged, dict):
            return ToolCall.from_dict(staged)
        return None

    @classmethod
    def from_result(cls, result: Any, call: ToolCall | None = None) -> Interrupt:
        """The park fact for a suspended ToolResult: an explicit kind wins;
        an approval id means approve; anything else let go of the process
        awaiting the world (await_external). The staged call and the reason
        ride the payload."""
        kind = (
            InterruptKind(result.interrupt_kind)
            if result.interrupt_kind
            else (
                InterruptKind.APPROVE
                if result.approval_request_id
                else InterruptKind.AWAIT_EXTERNAL
            )
        )
        payload: dict[str, Any] = {"reason": result.reason, "tool": str(result.tool)}
        if call is not None:
            payload["call"] = call.to_dict()
        return cls(
            kind=kind,
            request_id=result.approval_request_id or "",
            payload=payload,
        )
