"""Event sourcing primitives — Event, PlanEventType, and the hybrid restore reducer.

File-backed EventLog implementation lives in prodagent.backends.file.event_log.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING, Any

from prodagent.base.determinism import new_uuid4, now_wall

if TYPE_CHECKING:
    from collections.abc import Callable

    from prodagent.ports import CheckpointStore, EventLog

__all__ = ["Event", "PlanEventType", "RunEventType", "append_expected", "hybrid_restore"]


class PlanEventType(StrEnum):
    PLAN_CREATED = "PlanCreated"
    STEP_STARTED = "StepStarted"
    STEP_COMPLETED = "StepCompleted"
    STEP_FAILED = "StepFailed"
    STEP_SUSPENDED = "StepSuspended"
    PLAN_REPLANNED = "PlanReplanned"


class RunEventType(StrEnum):
    """REACTIVE-mode counterpart to ``PlanEventType`` — one entry per turn,
    plus terminal markers mirroring PLAN_FIRST's step lifecycle events."""

    TURN_COMPLETED = "TurnCompleted"
    RUN_COMPLETED = "RunCompleted"
    RUN_FAILED = "RunFailed"
    RUN_SUSPENDED = "RunSuspended"


class BoundaryEventType(StrEnum):
    """Boundary Q&A facts — what the outside world was asked and answered.

    The unified fact pipeline: every LLM response and
    every tool result a run received, recorded on the run's boundary stream
    (``<run_id>#boundary``) by the recording wrappers. These events are the
    raw material the cassette derives from, and the evidence behind the
    model-visible-is-log-derivable law. Turn/step markers keep their own
    stream untouched — the single-writer discipline there is unaffected
    because boundary facts flow on a separate stream."""

    LLM_RECORDED = "LlmRecorded"
    TOOL_RECORDED = "ToolRecorded"
    CLOCK_RECORDED = "ClockRecorded"


def boundary_stream(run_id: str) -> str:
    """Stream id of a run's boundary facts. ``#`` separates it from the
    marker stream the way ``::`` separates a child run — stream_id is the
    fan-out key, sibling streams are how concurrent writers stay single."""
    return f"{run_id}#boundary"


class SpanEventType(StrEnum):
    """Span facts — decision snapshots recorded on the run's spans stream.

    The projection criterion's truth side: a span is a
    fact of the run (recorded once, on ``<run_id>#spans``), and the
    exporter's output — spans.jsonl, a span table, an OTLP stream — is a
    cache over it, deletable and rebuildable (``rebuild_spans``)."""

    SPAN_RECORDED = "SpanRecorded"


def spans_stream(run_id: str) -> str:
    """Stream id of a run's span facts — sibling of the boundary stream."""
    return f"{run_id}#spans"


@dataclass
class Event:
    # ``event_type`` is ``str`` (not ``PlanEventType``) so the log serves any
    # event-sourced domain — plan execution passes a ``PlanEventType``, the
    # queue slice passes a ``QueueEventType``, REACTIVE passes a
    # ``RunEventType``. All are ``StrEnum`` <: ``str``.
    seq: int
    event_id: str
    event_type: str
    stream_id: str
    version: int
    timestamp: float
    data: dict[str, Any]

    @classmethod
    def make(cls, event_type: str, stream_id: str, version: int, **data: Any) -> Event:
        """Mint an event for appending. ``seq=0`` is the "unassigned"
        placeholder — the store's append is what stamps the real LSN."""
        return cls(
            seq=0,
            event_id=new_uuid4(),
            event_type=event_type,
            stream_id=stream_id,
            version=version,
            timestamp=now_wall(),
            data=data,
        )


async def append_expected(event_log: EventLog, event: Event, *, tail_seq: int) -> int:
    """Optimistic append: attach ``event`` after ``tail_seq``, return the seq
    the store assigned.

    The ``expected_seq`` tail-check is the discipline that makes an
    ``EventLog`` a single-writer stream: a concurrent appender (or a stale
    replay) moves the tail, the store raises ``VersionConflict``, and the
    caller finds out at the seam instead of interleaving two histories. This
    is the one home for that pattern — plan execution
    (``plan/event_log.py::PlanEventLog._record``) and a queue's durable
    projection (``the queue projection``) both delegate here,
    each advancing its own tail field with the returned seq.
    """
    return await event_log.append(event, expected_seq=tail_seq)


async def hybrid_restore(
    run_id: str,
    event_log: EventLog,
    checkpoint_store: CheckpointStore,
    reducer: Callable[[Any, Event], None],
    *,
    extract_base: Callable[[Any], tuple[Any, int, int] | None],
    empty_state: Callable[[], Any],
) -> tuple[Any, int, int]:
    """Checkpoint-as-base + exact tail replay, or full replay if no usable base.

    ``extract_base`` pulls ``(base_state, checkpoint_version, last_seq)`` out of
    a loaded checkpoint, or returns ``None`` if the checkpoint carries no
    resumable state for this domain — keeps this module blind to what a
    checkpoint's payload actually looks like (e.g. plan vs. run state).
    """
    run = await checkpoint_store.load(run_id)
    base = extract_base(run) if run is not None else None
    if base is not None:
        # Fast path: snapshot as the base, replay only the tail after it.
        state, checkpoint_version, last_seq = base
        post = await event_log.get_after(run_id, since_seq=last_seq)
        for event in post:
            reducer(state, event)  # fold the few events the snapshot missed
        return state, checkpoint_version, post[-1].seq if post else last_seq
    # No usable snapshot: fold the entire log from an empty state — slower,
    # but never wrong.
    fresh_state = empty_state()
    events = await event_log.get_events(run_id)
    for event in events:
        reducer(fresh_state, event)
    last_seq = events[-1].seq if events else 0
    return fresh_state, 0, last_seq
