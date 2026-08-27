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

from dataclasses import dataclass
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
    """One event as a JSON-able dict with a ``type`` discriminator."""
    kind = type(event).__name__
    if isinstance(event, ThinkTokenEvent):
        return {"type": kind, "token": event.token, "run_id": event.run_id}
    if isinstance(event, ToolCallStartEvent):
        return {"type": kind, "call": event.call.to_dict(), "run_id": event.run_id}
    if isinstance(event, ToolResultEvent):
        return {
            "type": kind,
            "name": event.name,
            "result": _wire_payload(event.result),
            "run_id": event.run_id,
        }
    if isinstance(event, StepStartedEvent):
        return {
            "type": kind,
            "step_id": event.step_id,
            "action": event.action,
            "run_id": event.run_id,
        }
    if isinstance(event, StepCompletedEvent):
        return {
            "type": kind,
            "step_id": event.step_id,
            "action": event.action,
            "result": _wire_payload(event.result),
            "run_id": event.run_id,
        }
    if isinstance(event, StepFailedEvent):
        return {
            "type": kind,
            "step_id": event.step_id,
            "action": event.action,
            "error": event.error,
            "run_id": event.run_id,
        }
    if isinstance(event, RunFailedEvent):
        return {"type": kind, "run": event.run.to_dict(), "error": event.error}
    if isinstance(event, (RunCompletedEvent, RunSuspendedEvent)):
        return {"type": kind, "run": event.run.to_dict()}
    raise TypeError(f"not an AgentEvent: {event!r}")


def event_from_wire(d: JsonDict) -> AgentEvent:
    """Rebuild an event from its wire dict — the inverse of :func:`event_to_wire`
    for JSON-able payloads."""
    from prodagent.kernel.state import AgentRun
    from prodagent.kernel.types import ToolCall

    kind = d.get("type")
    if kind == "ThinkTokenEvent":
        return ThinkTokenEvent(token=d["token"], run_id=d["run_id"])
    if kind == "ToolCallStartEvent":
        return ToolCallStartEvent(call=ToolCall.from_dict(d["call"]), run_id=d["run_id"])
    if kind == "ToolResultEvent":
        return ToolResultEvent(name=d["name"], result=d["result"], run_id=d["run_id"])
    if kind == "StepStartedEvent":
        return StepStartedEvent(step_id=d["step_id"], action=d["action"], run_id=d["run_id"])
    if kind == "StepCompletedEvent":
        return StepCompletedEvent(
            step_id=d["step_id"], action=d["action"], result=d["result"], run_id=d["run_id"]
        )
    if kind == "StepFailedEvent":
        return StepFailedEvent(
            step_id=d["step_id"], action=d["action"], error=d["error"], run_id=d["run_id"]
        )
    if kind == "RunCompletedEvent":
        return RunCompletedEvent(run=AgentRun.from_dict(d["run"]))
    if kind == "RunFailedEvent":
        return RunFailedEvent(run=AgentRun.from_dict(d["run"]), error=d["error"])
    if kind == "RunSuspendedEvent":
        return RunSuspendedEvent(run=AgentRun.from_dict(d["run"]))
    raise ValueError(f"unknown event type on the wire: {kind!r}")
