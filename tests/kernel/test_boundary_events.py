"""Boundary-recording laws — the unified fact pipeline.

Law 1 (the headline): everything the model saw is derivable from the WAL —
every LLM call's fingerprint observed by the client appears as an
``LlmRecorded`` event on the run's boundary stream, and every dispatched
tool lands as a ``ToolRecorded`` event. One fingerprint identity
(``cache_key_for``) spans cache hits, log lookup, and future cassette
derivation.

Law 2: boundary facts live on a sibling stream (``<run_id>#boundary``), so
the marker stream's single-writer discipline and seq assignment are
untouched — markers keep their own clean seq space regardless of
interleaved facts.

Law 3: calls outside any run scope record nothing (background work is not
a fact of a run).
"""

from __future__ import annotations

from typing import Any

from prodagent.backends.memory.blob import InMemoryBlobStore
from prodagent.backends.memory.event_log import InMemoryEventLog
from prodagent.base.blobs import BLOB_REF_KEY, fetch_ref
from prodagent.base.event_log import BoundaryEventType, RunEventType, boundary_stream
from prodagent.base.run_context import current_run_id
from prodagent.kernel.types import LLMResponse, SideEffectLevel, ToolMeta
from prodagent.llm.cache import cache_key_for
from prodagent.llm.fake import FakeLLMAdapter, script
from prodagent.llm.recording import RecordingLLMClient
from prodagent.plan.planner import Planner
from prodagent.runtime.agent_loop import agent_scheduler as reactive_scheduler
from prodagent.tooling.base import FunctionTool
from prodagent.tooling.dispatcher import ToolDispatcher


def _tool(name: str) -> FunctionTool:
    async def fn(**_: Any) -> dict:
        return {"action": name}

    return FunctionTool(
        name=name,
        fn=fn,
        meta=ToolMeta(
            name=name,
            is_readonly=True,
            side_effect_level=SideEffectLevel.LOW,
        ),
        schema={
            "name": name,
            "description": name,
            "parameters": {"type": "object", "properties": {}},
        },
    )


class _HashSpy(FakeLLMAdapter):
    """FakeLLM that fingerprints every request it receives — the 'what the
    model actually saw' side of the law."""

    def __init__(self, turns: list[dict[str, Any]]) -> None:
        src = script(*turns)
        super().__init__(responses=list(src._queue))  # noqa: SLF001 — copy the scripted queue
        self.seen_hashes: list[str] = []

    async def complete(  # type: ignore[no-untyped-def]
        self, messages, *, system="", tools=None, config=None, on_chunk=None
    ) -> LLMResponse:
        self.seen_hashes.append(cache_key_for(messages, system=system, tools=tools, config=config))
        return await super().complete(
            messages, system=system, tools=tools, config=config, on_chunk=on_chunk
        )


async def _drive(loop: reactive_scheduler) -> str:
    """Run to terminal; return the run id the loop minted."""
    run_id: str | None = None
    async for event in loop.stream("do the thing"):
        run_id = getattr(event, "run_id", None) or run_id
    assert run_id is not None
    return run_id


async def test_law_model_visible_is_wal_derivable() -> None:
    log = InMemoryEventLog()
    spy = _HashSpy([{"tool": "probe", "params": {}}, {"content": "done"}])
    dispatcher = ToolDispatcher({"probe": _tool("probe")}, event_log=log)
    loop = reactive_scheduler(RecordingLLMClient(spy, log), dispatcher, event_log=log)
    run_id = await _drive(loop)

    boundary = await log.get_events(boundary_stream(run_id))
    llm_events = [e for e in boundary if e.event_type == BoundaryEventType.LLM_RECORDED]
    tool_events = [e for e in boundary if e.event_type == BoundaryEventType.TOOL_RECORDED]

    # Law, LLM side: exactly the calls the model received, same fingerprints,
    # in order — the WAL can reconstruct everything the model saw.
    assert [e.data["req_hash"] for e in llm_events] == spy.seen_hashes
    assert len(llm_events) == 2
    # The recorded request carries the full semantic ask (messages + config).
    assert llm_events[0].data["request"]["messages"]
    assert llm_events[0].data["response"]["stop_reason"]

    # Law, tool side: the dispatched call is a fact, outcome and all.
    assert len(tool_events) == 1
    assert tool_events[0].data["request"] == {"tool": "probe", "args": {}}
    assert tool_events[0].data["response"]["outcome"] == "ok"


async def test_law_boundary_stream_is_sibling_of_markers() -> None:
    log = InMemoryEventLog()
    spy = _HashSpy([{"tool": "probe", "params": {}}, {"content": "done"}])
    dispatcher = ToolDispatcher({"probe": _tool("probe")}, event_log=log)
    loop = reactive_scheduler(RecordingLLMClient(spy, log), dispatcher, event_log=log)
    run_id = await _drive(loop)

    markers = await log.get_events(run_id)
    boundary = await log.get_events(boundary_stream(run_id))
    # Marker stream: terminal markers only, seqs from 1, no boundary leakage.
    assert all(e.event_type in set(RunEventType) for e in markers)
    assert [e.seq for e in markers] == list(range(1, len(markers) + 1))
    # Boundary stream is a different stream, with its own seq space.
    assert all(e.stream_id == boundary_stream(run_id) for e in boundary)
    assert [e.seq for e in boundary] == list(range(1, len(boundary) + 1))


async def test_law_off_scope_calls_record_nothing() -> None:
    log = InMemoryEventLog()
    fake = script({"content": "background work"})
    client = RecordingLLMClient(fake, log)
    assert current_run_id() is None
    response = await client.complete([{"role": "user", "content": "hi"}])
    assert response.content == "background work"
    assert await log.get_events("anything#boundary") == []


async def test_plan_first_records_boundary_facts_through_dispatcher() -> None:
    """Mode uniformity: PLAN_FIRST tool facts land on the same boundary
    stream shape, recorded by the same dispatcher choke point."""
    from prodagent.backends.factory import in_memory_checkpoint_store
    from prodagent.kernel.graph import Node, Plan
    from prodagent.kernel.scheduler import Scheduler
    from prodagent.kernel.units import ToolUnit

    log = InMemoryEventLog()
    plan = Plan()
    plan.add_nodes([Node(node_id="s1", body=ToolUnit("probe"), params={})])
    dispatcher = ToolDispatcher({"probe": _tool("probe")}, event_log=log)
    executor = Scheduler(
        planner=Planner(script({"content": "unused — preset DAG needs no planner"})),
        tools=dispatcher.dispatch,
        dispatcher=dispatcher,
        event_log=log,
        checkpoint_store=in_memory_checkpoint_store(),
        initial_plan=plan,
    )
    run_id: str | None = None
    async for event in executor.stream("probe it"):
        run_id = getattr(event, "run_id", None) or run_id
    assert run_id is not None

    boundary = await log.get_events(boundary_stream(run_id))
    tool_events = [e for e in boundary if e.event_type == BoundaryEventType.TOOL_RECORDED]
    assert len(tool_events) == 1
    assert tool_events[0].data["request"]["tool"] == "probe"


async def test_oversized_tool_result_spills_to_blob_pointer() -> None:
    """Spill law: a fact too big for the log line leaves a digest
    pointer; the body round-trips whole; small facts stay inline."""
    big_body = "x" * 80_000 + "终点 sentinel"

    async def big_fn(**_: Any) -> str:
        return big_body

    big_tool = FunctionTool(
        name="big",
        fn=big_fn,
        meta=ToolMeta(name="big", is_readonly=True, side_effect_level=SideEffectLevel.LOW),
        schema={
            "name": "big",
            "description": "big",
            "parameters": {"type": "object", "properties": {}},
        },
    )
    log = InMemoryEventLog()
    blobs = InMemoryBlobStore()
    dispatcher = ToolDispatcher({"big": big_tool}, event_log=log, blob_store=blobs)
    spy = _HashSpy([{"tool": "big", "params": {}}, {"content": "done"}])
    loop = reactive_scheduler(RecordingLLMClient(spy, log, blobs=blobs), dispatcher, event_log=log)
    run_id = await _drive(loop)

    boundary = await log.get_events(boundary_stream(run_id))
    tool_event = next(e for e in boundary if e.event_type == BoundaryEventType.TOOL_RECORDED)
    value = tool_event.data["response"]["value"]
    assert isinstance(value, dict) and BLOB_REF_KEY in value, "big fact is a pointer"
    assert await fetch_ref(value, blobs) == big_body, "pointer resolves to the whole body"

    # Small facts stay inline — the threshold is not a blanket transformation.
    small = next(
        e
        for e in await log.get_events(boundary_stream(run_id))
        if e.event_type == BoundaryEventType.LLM_RECORDED
    )
    assert not isinstance(small.data["request"]["messages"], dict), "small facts inline"
