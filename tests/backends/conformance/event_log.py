"""Conformance tests for ``EventLog`` implementations."""

from __future__ import annotations

from collections.abc import Callable
from typing import TypeAlias

from prodagent.core.event_log import Event, PlanEventType
from prodagent.ports.event_log import EventLog

Factory: TypeAlias = Callable[[], EventLog]


def _event(plan_id: str, version: int, **data: object) -> Event:
    return Event.make(PlanEventType.STEP_COMPLETED, plan_id, version, **data)


async def run_event_log_conformance(make_store: Factory) -> None:
    store = make_store()

    e1 = _event("p1", 1, step_id="s1")
    e2 = _event("p1", 2, step_id="s2")
    seq1 = await store.append(e1)
    seq2 = await store.append(e2)
    assert seq2 > seq1, "append must return monotonic seq"

    events = await store.get_events("p1")
    assert [e.event_id for e in events] == [e1.event_id, e2.event_id]
    assert [e.seq for e in events] == [seq1, seq2]

    tail = await store.get_after("p1", seq1)
    assert [e.event_id for e in tail] == [e2.event_id]
    assert tail[0].seq > seq1


async def run_event_log_plan_isolation_conformance(make_store: Factory) -> None:
    """Events for one plan_id do not leak into another."""
    store = make_store()
    await store.append(_event("pa", 1))
    await store.append(_event("pb", 1))
    await store.append(_event("pa", 2))

    pa = await store.get_events("pa")
    pb = await store.get_events("pb")
    assert len(pa) == 2
    assert len(pb) == 1
    assert {e.plan_id for e in pa} == {"pa"}
    assert {e.plan_id for e in pb} == {"pb"}


async def run_event_log_empty_plan_conformance(make_store: Factory) -> None:
    store = make_store()
    assert await store.get_events("nope") == []
    assert await store.get_after("nope", 0) == []
