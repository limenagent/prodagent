from __future__ import annotations

import asyncio
from collections import deque
from typing import TYPE_CHECKING, Any

from prodagent.core.types import LLMResponse, MessageList, StopReason, ToolCall

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from prodagent.llm.base import LLMConfig


class FakeLLMAdapter:
    """Deterministic LLM adapter for testing and offline demos."""

    def __init__(
        self,
        responses: list[LLMResponse] | None = None,
        latency_ms: float = 0.0,
        default_content: str = "I have completed the task.",
    ) -> None:
        self._queue: deque[LLMResponse] = deque(responses or [])
        self._latency_ms = latency_ms
        self._default_content = default_content
        self._call_count = 0

    @property
    def call_count(self) -> int:
        return self._call_count

    async def complete(
        self,
        messages: MessageList,
        *,
        system: str | list[dict[str, Any]] = "",
        tools: list[dict[str, Any]] | None = None,
        config: LLMConfig | None = None,
        on_chunk: Callable[[str], Awaitable[None]] | None = None,
    ) -> LLMResponse:
        if self._latency_ms > 0:
            await asyncio.sleep(self._latency_ms / 1_000)

        self._call_count += 1

        if self._queue:
            response = self._queue.popleft()
        else:
            last_user = next(
                (m["content"] for m in reversed(messages) if m.get("role") == "user"),
                self._default_content,
            )
            response = LLMResponse(
                content=f"[FakeLLM] {last_user}",
                stop_reason=StopReason.END_TURN,
                input_tokens=50,
                output_tokens=10,
            )

        if on_chunk is not None and response.content:
            for word in response.content.split():
                await on_chunk(word + " ")
                await asyncio.sleep(0)

        return response


def script(*turns: dict[str, Any]) -> FakeLLMAdapter:
    responses: list[LLMResponse] = []
    for turn in turns:
        if "tool" in turn:
            responses.append(
                LLMResponse(
                    content="",
                    tool_calls=[ToolCall(name=turn["tool"], params=turn.get("params", {}))],
                    stop_reason=StopReason.TOOL_USE,
                )
            )
        elif "tools" in turn:
            calls = [ToolCall(name=t["name"], params=t.get("params", {})) for t in turn["tools"]]
            responses.append(
                LLMResponse(content="", tool_calls=calls, stop_reason=StopReason.TOOL_USE)
            )
        else:
            responses.append(
                LLMResponse(content=turn.get("content", ""), stop_reason=StopReason.END_TURN)
            )
    return FakeLLMAdapter(responses=responses)
