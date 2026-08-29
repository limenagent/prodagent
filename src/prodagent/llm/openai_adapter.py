"""OpenAI-compatible adapter — one client for every OpenAI-shaped endpoint.

The framework ships no vendor list: DeepSeek, Qwen, Moonshot, Groq, Ollama,
self-hosted gateways — anything speaking the chat-completions dialect works
through this file unchanged, because what varies (endpoint, model, price)
arrives as configuration, not code. Dialect notes the translation absorbs:
``prompt_tokens`` already *includes* cached tokens (the inverse of
Anthropic's convention — both adapters land on the same all-inclusive
canonical form), streaming fragments each tool call across deltas (name and
arguments arrive in pieces — accumulate, parse once), and stop vocabulary
maps onto the Anthropic-named ``StopReason`` the loop branches on.
"""

from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING, Any, cast

from prodagent.base.errors import ToolCallParseError
from prodagent.kernel.types import LLMResponse, Message, MessageList, StopReason, ToolCall
from prodagent.llm import LLMConfig, normalise_content
from prodagent.llm.http_retry import (
    DeliveryGuard,
    register_retryable_exceptions,
    with_http_retry,
)

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

logger = logging.getLogger(__name__)


class OpenAIAdapter:
    """Wraps the OpenAI Python SDK (and OpenAI-compatible endpoints)."""

    def __init__(
        self,
        api_key: str | None = None,
        base_url: str | None = None,
        default_config: LLMConfig | None = None,
        *,
        model: str | None = None,
        cost_per_million_input: float | None = None,
        cost_per_million_output: float | None = None,
    ) -> None:
        try:
            import openai
        except ImportError as exc:
            raise ImportError(
                "openai package is required for OpenAIAdapter. "
                "Install it with: pip install 'prodagent[openai]'"
            ) from exc

        self._client = openai.AsyncOpenAI(api_key=api_key, base_url=base_url)
        # The SDK's transport failures (APIConnectionError ⊃ APITimeoutError)
        # don't subclass httpx errors — register them or they never retry.
        register_retryable_exceptions(openai.APIConnectionError)
        if default_config is not None:
            self._default_config = default_config
        else:
            # Rates resolve via LLMConfig's pricing catalog when unset here.
            self._default_config = LLMConfig(
                model=model or "gpt-4o",
                cost_per_million_input=cost_per_million_input or 0.0,
                cost_per_million_output=cost_per_million_output or 0.0,
            )

    @property
    def default_config(self) -> LLMConfig:
        """The config used when a caller passes ``config=None``.

        Public so the kernel can adopt it as the run's LLMConfig (model name,
        pricing) without importing adapter types — see ReactiveLoop._build_step.
        """
        return self._default_config

    async def complete(
        self,
        messages: MessageList,
        *,
        system: str | list[dict[str, Any]] = "",
        tools: list[dict[str, Any]] | None = None,
        config: LLMConfig | None = None,
        on_chunk: Callable[[str], Awaitable[None]] | None = None,
    ) -> LLMResponse:
        """Canonical history → chat-completions wire → canonical response,
        under the shared guarded-retry wrapper."""
        cfg = config or self._default_config
        full_messages = self._build_messages(messages, system)
        guard = DeliveryGuard()
        safe_chunk = on_chunk

        if on_chunk is not None:

            async def _guarded_chunk(text: str) -> None:
                guard.mark()  # first delivery disqualifies transparent retries
                await on_chunk(text)

            safe_chunk = _guarded_chunk

        return await with_http_retry(
            lambda: self._stream(full_messages, tools=tools, cfg=cfg, on_chunk=safe_chunk),
            stream_guard=guard if on_chunk is not None else None,
        )

    def _build_messages(
        self, messages: MessageList, system: str | list[dict[str, Any]]
    ) -> MessageList:
        # cache_control is Anthropic-only; OpenAI caches server-side via prompt_tokens_details.
        full: MessageList = []
        if system:
            if isinstance(system, str):
                system_text = system
            else:
                system_text = "\n\n".join(
                    b.get("text", "") for b in system if b.get("type") == "text"
                )
            if system_text:
                full.append({"role": "system", "content": system_text})
        for msg in messages:
            content = msg.get("content")
            normalised = normalise_content(content, join_text_blocks=True)
            # "thinking" is framework vocabulary for the Anthropic round-trip,
            # not an OpenAI wire field — never let it leak onto the request.
            # The comprehension widens the TypedDict to a plain dict; the cast
            # just re-narrows it to what it still is — a Message minus one key.
            payload = {k: v for k, v in msg.items() if k != "thinking"}
            full.append(
                cast(
                    "Message",
                    {**payload, "content": normalised} if normalised is not content else payload,
                )
            )
        return full

    @staticmethod
    def _build_tools(tools: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Canonical schema → the ``function`` envelope this dialect wants;
        ``input_schema`` or ``parameters``, whichever the source carries."""
        return [
            {
                "type": "function",
                "function": {
                    "name": t["name"],
                    "description": t.get("description", ""),
                    "parameters": t.get("input_schema", t.get("parameters", {})),
                },
            }
            for t in tools
        ]

    async def _stream(
        self,
        messages: MessageList,
        *,
        tools: list[dict[str, Any]] | None,
        cfg: LLMConfig,
        on_chunk: Callable[[str], Awaitable[None]] | None,
    ) -> LLMResponse:
        """Consume the chunk stream once, accumulating text, reasoning,
        usage, and tool-call fragments; parse tool arguments only after the
        stream ends (they arrive in pieces)."""
        kwargs: dict[str, Any] = {
            "model": cfg.model,
            "messages": messages,
            "temperature": cfg.temperature,
            "max_tokens": cfg.max_tokens,
            "stream": True,
            "stream_options": {"include_usage": True},
        }
        if cfg.timeout_seconds is not None:
            kwargs["timeout"] = cfg.timeout_seconds

        if tools:
            kwargs["tools"] = self._build_tools(tools)
            kwargs["tool_choice"] = "auto"

        content_parts: list[str] = []
        reasoning_parts: list[str] = []
        tool_call_chunks: dict[int, dict[str, Any]] = {}
        finish_reason: str = "end_turn"
        usage_input: int = 0
        usage_output: int = 0
        usage_cache_read: int = 0
        model_name: str = cfg.model
        chunk_count = 0

        async for chunk in await self._client.chat.completions.create(**kwargs):
            chunk_count += 1

            if chunk.usage:
                # Usage rides the FINAL chunk (stream_options.include_usage);
                # keep overwriting — the last write is the real one.
                usage_input = chunk.usage.prompt_tokens or 0
                usage_output = chunk.usage.completion_tokens or 0
                details = getattr(chunk.usage, "prompt_tokens_details", None)
                if details is not None:
                    usage_cache_read = getattr(details, "cached_tokens", 0) or 0
            if not chunk.choices:
                continue  # usage-only chunks carry no content
            choice = chunk.choices[0]
            delta = choice.delta
            if choice.finish_reason:
                finish_reason = choice.finish_reason
            if chunk.model:
                model_name = chunk.model

            if delta.content:
                content_parts.append(delta.content)
                if on_chunk is not None:
                    await on_chunk(delta.content)  # stream text out as it arrives

            # o1/o3/deepseek-r1-style models stream reasoning_content separately
            delta_reasoning = getattr(delta, "reasoning_content", "") or ""
            if delta_reasoning:
                reasoning_parts.append(delta_reasoning)

            # The wire fragments one tool call across many deltas (name and
            # arguments arrive in pieces, keyed by index) — accumulate, parse once.
            for tc_delta in delta.tool_calls or []:
                idx = tc_delta.index
                if idx not in tool_call_chunks:
                    tool_call_chunks[idx] = {
                        "id": tc_delta.id or "",
                        "name": "",
                        "arguments": "",
                    }
                if tc_delta.function:
                    if tc_delta.function.name:
                        tool_call_chunks[idx]["name"] += tc_delta.function.name
                    if tc_delta.function.arguments:
                        tool_call_chunks[idx]["arguments"] += tc_delta.function.arguments

        tool_calls = []
        for tc in tool_call_chunks.values():
            raw_args = tc["arguments"] or "{}"
            try:
                params = json.loads(raw_args)
            except json.JSONDecodeError as exc:
                raise ToolCallParseError(
                    f"OpenAI tool call {tc['name']!r} had non-JSON arguments",
                    tool_name=tc["name"],
                    args_fragment=raw_args[:200],
                ) from exc
            tool_calls.append(ToolCall(name=tc["name"], params=params, call_id=tc["id"]))

        return LLMResponse(
            content="".join(content_parts),
            tool_calls=tool_calls,
            stop_reason=_map_stop_reason(finish_reason),
            # OpenAI counts cached tokens INSIDE prompt_tokens — the inverse of
            # Anthropic's convention; both funnel into the same all-inclusive
            # canonical form the cost math subtracts from.
            input_tokens=usage_input,
            output_tokens=usage_output,
            model=model_name,
            cache_read_tokens=usage_cache_read,
            reasoning_content="".join(reasoning_parts),
        )


# OpenAI finish_reason → Anthropic stop_reason (canonical form the framework speaks).
_OPENAI_STOP_MAP: dict[str, StopReason] = {
    "stop": StopReason.END_TURN,
    "tool_calls": StopReason.TOOL_USE,
    "length": StopReason.MAX_TOKENS,
    "content_filter": StopReason.CONTENT_FILTER,
    "function_call": StopReason.TOOL_USE,
}


def _map_stop_reason(finish_reason: str | None) -> StopReason:
    """finish_reason → canonical StopReason; unknowns coerce to END_TURN
    instead of crashing the loop on a provider's new value."""
    if not finish_reason:
        return StopReason.END_TURN
    return _OPENAI_STOP_MAP.get(finish_reason, StopReason.coerce(finish_reason))
