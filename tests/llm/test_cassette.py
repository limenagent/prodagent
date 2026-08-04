from __future__ import annotations

import pytest

from prodagent.core.types import LLMResponse, ToolCall
from prodagent.evaluation.testing.cassette import RecordingLLMClient, ReplayLLMClient


class _StubLLM:
    def __init__(self, responses: list[LLMResponse]) -> None:
        self._responses = responses
        self._i = 0

    async def complete(self, messages, *, system="", tools=None, config=None, on_chunk, **_):
        r = self._responses[self._i]
        self._i += 1
        return r


async def _noop(_chunk: str) -> None:
    pass


@pytest.mark.asyncio
async def test_record_then_replay_roundtrip(tmp_path):
    cassette = tmp_path / "incident.jsonl"
    scripted = [
        LLMResponse(
            content="",
            tool_calls=[ToolCall(name="query_order", params={"id": "O1"})],
            stop_reason="tool_use",
        ),
        LLMResponse(content="refund done", stop_reason="end_turn"),
    ]

    rec = RecordingLLMClient(_StubLLM(scripted), cassette=cassette)
    r1 = await rec.complete(
        [{"role": "user", "content": "refund O1"}], system="sys", on_chunk=_noop
    )
    r2 = await rec.complete([{"role": "user", "content": "next"}], on_chunk=_noop)
    rec.close()

    assert cassette.exists()
    assert r1.tool_calls[0].name == "query_order"
    assert r2.content == "refund done"

    replay = ReplayLLMClient(cassette=cassette)
    p1 = await replay.complete([{"role": "user", "content": "anything"}], on_chunk=_noop)
    p2 = await replay.complete([{"role": "user", "content": "anything"}], on_chunk=_noop)
    assert p1.tool_calls[0].name == "query_order"
    assert p1.tool_calls[0].params == {"id": "O1"}
    assert p2.content == "refund done"


@pytest.mark.asyncio
async def test_replay_exhaustion_raises(tmp_path):
    cassette = tmp_path / "short.jsonl"
    rec = RecordingLLMClient(
        _StubLLM([LLMResponse(content="only one", stop_reason="end_turn")]),
        cassette=cassette,
    )
    await rec.complete([{"role": "user", "content": "go"}], on_chunk=_noop)
    rec.close()

    replay = ReplayLLMClient(cassette=cassette)
    await replay.complete([{"role": "user", "content": "go"}], on_chunk=_noop)
    with pytest.raises(IndexError, match="exhausted"):
        await replay.complete([{"role": "user", "content": "more"}], on_chunk=_noop)


@pytest.mark.asyncio
async def test_replay_streams_content_to_on_chunk(tmp_path):
    cassette = tmp_path / "stream.jsonl"
    rec = RecordingLLMClient(
        _StubLLM([LLMResponse(content="hello world", stop_reason="end_turn")]),
        cassette=cassette,
    )
    await rec.complete([{"role": "user", "content": "hi"}], on_chunk=_noop)
    rec.close()

    chunks: list[str] = []

    async def _capture(chunk: str) -> None:
        chunks.append(chunk)

    replay = ReplayLLMClient(cassette=cassette)
    await replay.complete([{"role": "user", "content": "hi"}], on_chunk=_capture)
    assert chunks == ["hello world"]
