"""Frozen-clock laws — replayed time comes from the tape, never the wall.

Law 1 (recording): a recorded run's clock asks land as facts on the
boundary stream and ride the cassette as ``kind="clock"`` records.

Law 2 (frozen answers): the frozen clock serves each port's readings in
the recorded order, and past the tape's end it CLAMPS to the last reading
— a frozen clock that advanced would be a lie, and a replay that asked
more clock questions than its recording still stays deterministic.

Law 3 (offline): no reading the frozen clock ever returns comes from the
real clock — every value is one the tape already held.
"""

from __future__ import annotations

from typing import Any

from prodagent.backends.memory.event_log import InMemoryEventLog
from prodagent.base.determinism import now_wall, override
from prodagent.base.event_log import BoundaryEventType, boundary_stream
from prodagent.kernel.types import SideEffectLevel, ToolMeta
from prodagent.llm.fake import script
from prodagent.llm.recording import RecordingLLMClient
from prodagent.plan.scheduler import reactive_scheduler
from prodagent.replay.cassette import derive_cassette
from prodagent.replay.engine import FrozenClock
from prodagent.tooling.base import FunctionTool
from prodagent.tooling.dispatcher import ToolDispatcher


def _tool(name: str) -> FunctionTool:
    async def fn(**_: Any) -> dict:
        return {"action": name}

    return FunctionTool(
        name=name,
        fn=fn,
        meta=ToolMeta(name=name, is_readonly=True, side_effect_level=SideEffectLevel.LOW),
        schema={
            "name": name,
            "description": name,
            "parameters": {"type": "object", "properties": {}},
        },
    )


async def _live_run() -> tuple[Any, InMemoryEventLog]:
    log = InMemoryEventLog()
    dispatcher = ToolDispatcher({"probe": _tool("probe")}, event_log=log)
    loop = reactive_scheduler(
        RecordingLLMClient(script({"tool": "probe", "params": {}}, {"content": "done"}), log),
        dispatcher,
        event_log=log,
    )
    run = None
    async for event in loop.stream("do the thing"):
        run = getattr(event, "run", None) or run
    return run, log


async def test_recording_law_clock_asks_become_cassette_records() -> None:
    run, log = await _live_run()
    facts = await log.get_events(boundary_stream(run.run_id))
    clock_facts = [e for e in facts if e.event_type == BoundaryEventType.CLOCK_RECORDED]
    assert clock_facts, "the run's time asks landed as facts"
    assert all("port" in e.data and "value" in e.data for e in clock_facts)

    cassette = await derive_cassette(log, run.run_id)
    clock_records = [r for r in cassette.records if r.kind == "clock"]
    assert len(clock_records) == len(clock_facts)
    assert [r.response["value"] for r in clock_records] == [e.data["value"] for e in clock_facts]


async def test_frozen_clock_answers_in_order_then_clamps() -> None:
    _run, log = await _live_run()
    run_id = _run.run_id
    cassette = await derive_cassette(log, run_id)
    wall_values = [
        r.response["value"]
        for r in cassette.records
        if r.kind == "clock" and r.response["port"] == "wall"
    ]
    assert wall_values, "the tape holds wall readings"

    frozen = FrozenClock(cassette)
    for expected in wall_values:
        assert frozen.wall() == expected, "recorded order, per port"
    # Past the tape: clamp to the last reading, forever frozen.
    last = wall_values[-1]
    assert frozen.wall() == last and frozen.wall() == last
    assert frozen.wall() != now_wall() or last == now_wall()  # never the live clock


async def test_frozen_clock_is_offline_and_deterministic() -> None:
    _run, log = await _live_run()
    cassette = await derive_cassette(log, _run.run_id)
    # Two fresh replays (fresh frozen clocks) drawing the same number of
    # readings see the same time — the whole point of freezing.
    with override(time_port=FrozenClock(cassette)):
        first = [now_wall() for _ in range(5)]
    with override(time_port=FrozenClock(cassette)):
        second = [now_wall() for _ in range(5)]
    assert first == second
    # And past the tape's end both clamp to the same frozen instant.
    assert first[-1] == second[-1]
