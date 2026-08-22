from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from prodagent.llm import LLMConfig
from prodagent.llm.openai_adapter import OpenAIAdapter


def _streaming_client(*, content: str = "hello", model: str = "glm-5") -> MagicMock:
    delta = MagicMock()
    delta.content = content
    delta.tool_calls = None
    delta.reasoning_content = None
    choice = MagicMock()
    choice.delta = delta
    choice.finish_reason = "stop"
    chunk = MagicMock()
    chunk.choices = [choice]
    chunk.model = model
    chunk.usage = MagicMock(
        prompt_tokens=10,
        completion_tokens=5,
        prompt_tokens_details=MagicMock(cached_tokens=0),
    )

    async def _astream():
        yield chunk

    async def _create(**kwargs):
        return _astream()

    client = MagicMock()
    client.chat = MagicMock()
    client.chat.completions = MagicMock()
    client.chat.completions.create = AsyncMock(side_effect=_create)
    return client


async def _noop(_chunk: str) -> None:
    pass


@pytest.mark.asyncio
async def test_complete_always_streams_over_wire():
    client = _streaming_client(content="batch result")
    adapter = OpenAIAdapter(api_key="dummy", model="glm-5")
    adapter._client = client

    response = await adapter.complete(
        [{"role": "user", "content": "hi"}],
        config=LLMConfig(model="glm-5", max_tokens=100, temperature=0.0),
        on_chunk=_noop,
    )

    assert response.content == "batch result"
    client.chat.completions.create.assert_called_once()
    call_kwargs = client.chat.completions.create.call_args.kwargs
    assert call_kwargs.get("stream") is True, (
        "complete() must stream over the wire (Zhipu rejects non-streaming create)"
    )


@pytest.mark.asyncio
async def test_complete_returns_full_response_shape():
    client = _streaming_client(content="hello world", model="glm-5")
    adapter = OpenAIAdapter(api_key="dummy", model="glm-5")
    adapter._client = client

    response = await adapter.complete(
        [{"role": "user", "content": "hi"}],
        config=LLMConfig(model="glm-5", max_tokens=100, temperature=0.0),
        on_chunk=_noop,
    )

    assert response.content == "hello world"
    assert response.stop_reason == "end_turn"
    assert response.model == "glm-5"
    assert response.input_tokens == 10
    assert response.output_tokens == 5
    assert response.tool_calls == []


@pytest.mark.asyncio
async def test_complete_awaits_async_on_chunk_per_chunk():
    client = _streaming_client(content="chunked")
    adapter = OpenAIAdapter(api_key="dummy", model="glm-5")
    adapter._client = client

    chunks: list[str] = []

    async def _capture(chunk: str) -> None:
        chunks.append(chunk)

    response = await adapter.complete(
        [{"role": "user", "content": "hi"}],
        config=LLMConfig(model="glm-5", max_tokens=100, temperature=0.0),
        on_chunk=_capture,
    )

    assert response.content == "chunked"
    assert chunks == ["chunked"], "async on_chunk callback must be awaited per chunk"
