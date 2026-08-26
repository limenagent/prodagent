"""Event sourcing primitives — Event, PlanEventType, and the hybrid restore reducer.

File-backed EventLog implementation lives in prodagent.backends.file.event_log.
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Callable

    from prodagent.ports import CheckpointStore, EventLog

__all__ = ["Event", "PlanEventType", "RunEventType", "hybrid_restore"]


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


@dataclass
class Event:
    # ``event_type`` is ``str`` (not ``PlanEventType``) so the log serves any
    # event-sourced domain — plan execution passes a ``PlanEventType``, the
    # WorkQueue slice passes a ``QueueEventType``, REACTIVE passes a
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
        return cls(
            seq=0,
            event_id=str(uuid.uuid4()),
            event_type=event_type,
            stream_id=stream_id,
            version=version,
            timestamp=time.time(),
            data=data,
        )


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
        state, checkpoint_version, last_seq = base
        post = await event_log.get_after(run_id, since_seq=last_seq)
        for event in post:
            reducer(state, event)
        return state, checkpoint_version, post[-1].seq if post else last_seq
    fresh_state = empty_state()
    events = await event_log.get_events(run_id)
    for event in events:
        reducer(fresh_state, event)
    last_seq = events[-1].seq if events else 0
    return fresh_state, 0, last_seq
