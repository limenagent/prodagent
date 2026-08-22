from __future__ import annotations

import asyncio
from collections import deque
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from prodagent.core.types import LLMResponse, MessageList, StopReason, ToolCall

if TYPE_CHECKING:
    from collections.abc import Awaitable, Mapping, Sequence

    from prodagent.llm import LLMConfig


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


# A queued answer: a fixed response, or one computed from the live message
# list (for trajectories that depend on conversation history).
ResponseSource = LLMResponse | Callable[[MessageList], LLMResponse]


class _AgentQueue:
    """Per-route FIFO of responses (or per-call response factories)."""

    def __init__(self, sources: Sequence[ResponseSource] = ()) -> None:
        self._sources: list[ResponseSource] = list(sources)

    async def complete(
        self,
        messages: MessageList,
        *,
        on_chunk: Callable[[str], Awaitable[None]] | None = None,
    ) -> LLMResponse:
        # A static LLMResponse is a scripted turn: consume it (FIFO). A
        # callable is a standing responder: it answers every call — popping
        # it would silently degrade history-dependent routes to the echo
        # fallback after one turn.
        response: LLMResponse | None = None
        if self._sources:
            source = self._sources[0]
            if callable(source):
                response = source(messages)
            else:
                self._sources.pop(0)
                response = source
        if response is None:
            last_user = next(
                (m["content"] for m in reversed(messages) if m.get("role") == "user"),
                "(no user message)",
            )
            response = LLMResponse(
                content=f"[fallback] {last_user}",
                stop_reason=StopReason.END_TURN,
                input_tokens=50,
                output_tokens=10,
            )
        if response.content and on_chunk is not None:
            for word in response.content.split():
                await on_chunk(word + " ")
                await asyncio.sleep(0)
        return response


class RoutingFakeLLM:
    """One fake LLM shared by a parent and its spawned peers, routed per agent.

    A single response queue cannot serve concurrent agents — spawn fan-outs
    pop responses in nondeterministic order. This adapter routes each
    ``complete()`` call to a per-key FIFO by sniffing the system prompt:

    - ``add(name, responses)`` anchors on the ``"# {name} Agent"`` header
      that :meth:`Agent.build_system_prompt` emits;
    - ``add_route(marker, responses)`` anchors on any system-prompt
      substring (planner prompts, tool-side analyst prompts, ...).

    First matching route wins (registration order); unmatched calls fall to
    the default queue. Queue entries may be :class:`LLMResponse` objects
    (scripted turns, consumed FIFO) or ``Callable[[MessageList], LLMResponse]``
    (standing responders — invoked on every call, never consumed).
    """

    def __init__(
        self,
        *,
        default: Sequence[ResponseSource] = (),
        routes: Mapping[str, Sequence[ResponseSource]] | None = None,
    ) -> None:
        self._queues: dict[str, _AgentQueue] = {}
        self._default = _AgentQueue(default)
        self._call_count = 0
        for marker, sources in (routes or {}).items():
            self.add_route(marker, sources)

    @property
    def call_count(self) -> int:
        return self._call_count

    def add(self, agent_name: str, responses: Sequence[ResponseSource]) -> _AgentQueue:
        """Route on the ``# {agent_name} Agent`` system-prompt header."""
        return self.add_route(f"# {agent_name} Agent", responses)

    def add_route(self, marker: str, responses: Sequence[ResponseSource]) -> _AgentQueue:
        """Route on an arbitrary system-prompt substring."""
        queue = _AgentQueue(responses)
        self._queues[marker] = queue
        return queue

    def set_default(self, responses: Sequence[ResponseSource]) -> _AgentQueue:
        self._default = _AgentQueue(responses)
        return self._default

    def _resolve(self, system: str) -> _AgentQueue:
        for marker, queue in self._queues.items():
            if marker in system:
                return queue
        return self._default

    async def complete(
        self,
        messages: MessageList,
        *,
        system: str | list[dict[str, Any]] = "",
        tools: list[dict[str, Any]] | None = None,
        config: LLMConfig | None = None,
        on_chunk: Callable[[str], Awaitable[None]] | None = None,
    ) -> LLMResponse:
        self._call_count += 1
        system_str = system if isinstance(system, str) else str(system)
        queue = self._resolve(system_str)
        return await queue.complete(messages, on_chunk=on_chunk)
