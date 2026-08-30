"""Cassette laws — the tape is a projection, portable, and dual-keyed.

Law 1 (derivation): a live run's cassette carries exactly its boundary
facts, in order — llm and tool kinds, the same request fingerprints the
recorder logged.

Law 2 (round trip): ``to_jsonl`` then ``from_jsonl`` returns the identical
cassette — the tape survives its file form.

Law 3 (self-containment): facts that spilled to the blob store come back
inline — a travelling tape carries every body inside it.

Law 4 (dual-key matching): position alone is not consent — the content
fingerprint must agree, and a divergence is named with both sides.

Law 5 (format discipline): JSONL — one object per line, header first,
lines split on "\\n" only.
"""

from __future__ import annotations

from typing import Any

from prodagent.backends.memory.blob import InMemoryBlobStore
from prodagent.backends.memory.event_log import InMemoryEventLog
from prodagent.base.event_log import BoundaryEventType, boundary_stream
from prodagent.kernel.loop import ReactiveLoop
from prodagent.kernel.types import LLMResponse, SideEffectLevel, ToolMeta
from prodagent.llm.cache import cache_key_for
from prodagent.llm.fake import FakeLLMAdapter, script
from prodagent.llm.recording import RecordingLLMClient
from prodagent.replay.cassette import (
    Cassette,
    CassetteMismatch,
    derive_cassette,
    tool_request_hash,
)
from prodagent.tooling.base import FunctionTool
from prodagent.tooling.dispatcher import ToolDispatcher


def _tool(name: str, *, big: bool = False) -> FunctionTool:
    async def fn(**_: Any) -> dict | str:
        if big:
            return "y" * 80_000 + "end sentinel"
        return {"action": name}

    return FunctionTool(
        name=name,
        fn=fn,
        meta=ToolMeta(name=name, is_readonly=True, side_effect_level=SideEffectLevel.LOW),
        schema={"name": name, "description": name, "parameters": {"type": "object", "properties": {}}},
    )


class _HashSpy(FakeLLMAdapter):
    def __init__(self, turns: list[dict[str, Any]]) -> None:
        src = script(*turns)
        super().__init__(responses=list(src._queue))  # noqa: SLF001 — copy the scripted queue
        self.seen_hashes: list[str] = []

    async def complete(  # type: ignore[no-untyped-def]
        self, messages, *, system="", tools=None, config=None, on_chunk=None
    ) -> LLMResponse:
        self.seen_hashes.append(cache_key_for(messages, system=system, tools=tools, config=config))
        return await super().complete(messages, system=system, tools=tools, config=config,
                                      on_chunk=on_chunk)


async def _drive(log, blobs, turns, tools):
    spy = _HashSpy(turns)
    dispatcher = ToolDispatcher(
        {t.name: t for t in tools}, event_log=log, blob_store=blobs
    )
    loop = ReactiveLoop(RecordingLLMClient(spy, log, blobs=blobs), dispatcher, event_log=log)
    run_id: str | None = None
    async for event in loop.stream("do the thing"):
        run_id = getattr(event, "run_id", None) or run_id
    assert run_id is not None
    return run_id, spy


async def test_derivation_law_cassette_equals_boundary_facts() -> None:
    log = InMemoryEventLog()
    run_id, spy = await _drive(
        log, None, [{"tool": "probe", "params": {}}, {"content": "done"}], [_tool("probe")]
    )
    cassette = await derive_cassette(log, run_id)

    facts = await log.get_events(boundary_stream(run_id))
    assert len(cassette.records) == len(facts)
    assert [r.seq for r in cassette.records] == list(range(1, len(facts) + 1))
    # Clock facts ride along (the loop records its time asks); the decision
    # kinds keep their order.
    decision_kinds = [r.kind for r in cassette.records if r.kind != "clock"]
    assert decision_kinds == ["llm", "tool", "llm"]
    assert any(r.kind == "clock" for r in cassette.records), "clock facts landed"
    llm_records = [r for r in cassette.records if r.kind == "llm"]
    assert [r.req_hash for r in llm_records] == spy.seen_hashes, "same fingerprints as asked"
    tool_record = next(r for r in cassette.records if r.kind == "tool")
    assert tool_record.req_hash == tool_request_hash({"tool": "probe", "args": {}})
    assert cassette.header.run_id == run_id
    assert cassette.header.tool_manifest == ["probe"]


async def test_round_trip_law_jsonl_returns_identical_cassette(tmp_path) -> None:
    log = InMemoryEventLog()
    run_id, _ = await _drive(
        log, None, [{"tool": "probe", "params": {}}, {"content": "done"}], [_tool("probe")]
    )
    cassette = await derive_cassette(log, run_id, config_hash="a3f9c1")

    cassette.save(tmp_path / "tape.jsonl")
    loaded = Cassette.load(tmp_path / "tape.jsonl")

    assert loaded.header == cassette.header
    assert loaded.records == cassette.records
    # The header line is line one and carries the schema version.
    first = (tmp_path / "tape.jsonl").read_text(encoding="utf-8").split("\n")[0]
    assert '"header": true' in first and '"schema_version": 1' in first


async def test_self_containment_law_spilled_bodies_come_back_inline() -> None:
    log = InMemoryEventLog()
    blobs = InMemoryBlobStore()
    run_id, _ = await _drive(
        log, blobs, [{"tool": "big", "params": {}}, {"content": "done"}], [_tool("big", big=True)]
    )
    # The fact on the WAL is a pointer, not the body.
    facts = await log.get_events(boundary_stream(run_id))
    tool_fact = next(e for e in facts if e.event_type == BoundaryEventType.TOOL_RECORDED)
    assert isinstance(tool_fact.data["response"]["value"], dict)

    cassette = await derive_cassette(log, run_id, blobs=blobs)
    tool_record = next(r for r in cassette.records if r.kind == "tool")
    assert isinstance(tool_record.response["value"], str)
    assert tool_record.response["value"].endswith("end sentinel")


async def test_dual_key_law_position_needs_fingerprint_consent() -> None:
    log = InMemoryEventLog()
    run_id, _ = await _drive(
        log, None, [{"tool": "probe", "params": {}}, {"content": "done"}], [_tool("probe")]
    )
    cassette = await derive_cassette(log, run_id)

    record = cassette.match(2, tool_request_hash({"tool": "probe", "args": {}}))
    assert record.kind == "tool"

    try:
        cassette.match(2, "0" * 64)
    except CassetteMismatch as exc:
        assert "position 2" in str(exc) and "tape" in str(exc), "both sides named"
    else:
        raise AssertionError("fingerprint disagreement must raise")
    try:
        cassette.match(99, "0" * 64)
    except CassetteMismatch:
        pass
    else:
        raise AssertionError("missing position must raise")
