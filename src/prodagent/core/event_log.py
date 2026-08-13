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

__all__ = ["Event", "PlanEventType", "hybrid_restore"]


class PlanEventType(StrEnum):
    PLAN_CREATED = "PlanCreated"
    STEP_STARTED = "StepStarted"
    STEP_COMPLETED = "StepCompleted"
    STEP_FAILED = "StepFailed"
    STEP_SUSPENDED = "StepSuspended"
    PLAN_REPLANNED = "PlanReplanned"


@dataclass
class Event:
    # ``event_type`` is ``str`` (not ``PlanEventType``) so the log serves any
    # event-sourced domain — plan execution passes a ``PlanEventType``, the
    # WorkQueue slice passes a ``QueueEventType``. Both are ``StrEnum`` <: ``str``.
    seq: int
    event_id: str
    event_type: str
    plan_id: str
    version: int
    timestamp: float
    data: dict[str, Any]

    @classmethod
    def make(cls, event_type: str, plan_id: str, version: int, **data: Any) -> Event:
        return cls(
            seq=0,
            event_id=str(uuid.uuid4()),
            event_type=event_type,
            plan_id=plan_id,
            version=version,
            timestamp=time.time(),
            data=data,
        )


async def hybrid_restore(
    run_id: str,
    event_log: EventLog,
    checkpoint_store: CheckpointStore,
    reducer: Callable[[dict[str, Any], Event], None],
) -> tuple[dict[str, Any], int, int]:
    run = await checkpoint_store.load(run_id)
    if run is not None and run.plan_state is not None:
        state = run.plan_state
        post = await event_log.get_after(run_id, since_seq=run.plan_last_seq)
        for event in post:
            reducer(state, event)
        last_seq = post[-1].seq if post else run.plan_last_seq
        return state, run.checkpoint_version, last_seq
    fresh_state: dict[str, Any] = {"steps": {}, "version": 0}
    events = await event_log.get_events(run_id)
    for event in events:
        reducer(fresh_state, event)
    last_seq = events[-1].seq if events else 0
    return fresh_state, 0, last_seq
