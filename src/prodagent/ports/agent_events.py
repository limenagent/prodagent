"""AgentEvent — the stream vocabulary of an agent run, and its wire codec.

Lifted from ``kernel/types.py``: these events are the wire contract of every
stream consumer — the LeafExecutor and RunnerPort outputs, the playground,
and (next) the remote plane. ``kernel/types`` re-exports them so kernel
consumers keep one import site, same precedent as the base-vocabulary
re-exports there.

Type references into the kernel are annotation-only (``TYPE_CHECKING``)
except in the codec, where ``ToolCall``/``AgentRun`` reconstruction goes
through function-body imports — the repo's sanctioned lazy-resolution
mechanism; layering pins module-level edges only.

Codec policy: :func:`event_to_wire` yields a JSON-able dict with a ``type``
discriminator. Object payloads (a tool result, a step result) pass through
when JSON-able, dataclasses with ``to_dict`` are converted, and anything
else is stringified — the wire serves transport and observability, so
round-trip (:func:`event_from_wire`) is lossless for JSON-able payloads and
degrades to text for opaque ones. ``RunCompletedEvent``/``RunFailedEvent``/
``RunSuspendedEvent`` carry the run as ``AgentRun.to_dict()`` — the same
document a checkpoint stores.
"""

from __future__ import annotations

from dataclasses import dataclass, fields
from typing import TYPE_CHECKING, Any, TypeAlias

if TYPE_CHECKING:
    from prodagent.base.types import JsonDict
    from prodagent.kernel.state import AgentRun
    from prodagent.kernel.types import RunId, ToolCall, ToolName

__all__ = [
    "ThinkTokenEvent",
    "ToolCallStartEvent",
    "ToolResultEvent",
    "StepStartedEvent",
    "StepCompletedEvent",
    "StepFailedEvent",
    "RunCompletedEvent",
    "RunFailedEvent",
    "RunSuspendedEvent",
    "AgentEvent",
    "event_to_wire",
    "event_from_wire",
]


# ── Streaming events — the discriminated union yielded by every run stream ────


@dataclass(frozen=True, slots=True)
class ThinkTokenEvent:
    token: str
    run_id: RunId


@dataclass(frozen=True, slots=True)
class ToolCallStartEvent:
    call: ToolCall
    run_id: RunId


@dataclass(frozen=True, slots=True)
class ToolResultEvent:
    name: ToolName
    result: object
    run_id: RunId


@dataclass(frozen=True, slots=True)
class StepStartedEvent:
    step_id: str
    action: ToolName
    run_id: RunId


@dataclass(frozen=True, slots=True)
class StepCompletedEvent:
    step_id: str
    action: ToolName
    result: object
    run_id: RunId


@dataclass(frozen=True, slots=True)
class StepFailedEvent:
    """Triggers replan."""

    step_id: str
    action: ToolName
    error: str
    run_id: RunId


@dataclass(frozen=True, slots=True)
class RunCompletedEvent:
    run: AgentRun


@dataclass(frozen=True, slots=True)
class RunFailedEvent:
    """Terminated due to budget, loop-detection, or abort."""

    run: AgentRun
    error: str


@dataclass(frozen=True, slots=True)
class RunSuspendedEvent:
    run: AgentRun


AgentEvent: TypeAlias = (
    ThinkTokenEvent
    | ToolCallStartEvent
    | ToolResultEvent
    | StepStartedEvent
    | StepCompletedEvent
    | StepFailedEvent
    | RunCompletedEvent
    | RunFailedEvent
    | RunSuspendedEvent
)


# ── Wire codec ────────────────────────────────────────────────────────────────

_JSON_SCALARS = (str, int, float, bool, type(None))

_KINDS: dict[str, type] = {
    c.__name__: c
    for c in (
        ThinkTokenEvent,
        ToolCallStartEvent,
        ToolResultEvent,
        StepStartedEvent,
        StepCompletedEvent,
        StepFailedEvent,
        RunCompletedEvent,
        RunFailedEvent,
        RunSuspendedEvent,
    )
}


def _wire_payload(value: object) -> Any:
    """JSON-able form of an opaque event payload: pass primitives/containers
    through, convert ``to_dict`` dataclasses, stringify the rest."""
    if isinstance(value, _JSON_SCALARS):
        return value
    if isinstance(value, (dict, list)):
        return value
    to_dict = getattr(value, "to_dict", None)
    if callable(to_dict):
        return to_dict()
    return str(value)


def event_to_wire(event: AgentEvent) -> JsonDict:
    """One event as a JSON-able dict with a ``type`` discriminator.

    Field-driven: every payload is either a string (``run_id``, ``error``,
    ...) or an opaque object rendered by :func:`_wire_payload` — which is
    exactly what the per-event branches this replaced did."""
    cls = type(event)
    if cls not in _KINDS.values():
        raise TypeError(f"not an AgentEvent: {event!r}")
    d: JsonDict = {"type": cls.__name__}
    for f in fields(event):
        d[f.name] = _wire_payload(getattr(event, f.name))
    return d


def event_from_wire(d: JsonDict) -> AgentEvent:
    """Rebuild an event from its wire dict — the inverse of :func:`event_to_wire`
    for JSON-able payloads."""
    from typing import cast

    from prodagent.kernel.state import AgentRun
    from prodagent.kernel.types import ToolCall

    kind = d.get("type")
    cls = _KINDS.get(kind) if isinstance(kind, str) else None
    if cls is None:
        raise ValueError(f"unknown event type on the wire: {kind!r}")
    # Only two fields need reconstruction — the rest are wire scalars or
    # opaque payloads that round-trip as-is.
    rebuild = {"call": ToolCall.from_dict, "run": AgentRun.from_dict}
    kwargs = {
        f.name: rebuild[f.name](d[f.name]) if f.name in rebuild else d[f.name] for f in fields(cls)
    }
    return cast("AgentEvent", cls(**kwargs))
