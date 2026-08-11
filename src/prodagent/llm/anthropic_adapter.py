from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any, cast

import anthropic

from prodagent.core.exceptions import ToolCallParseError
from prodagent.core.types import LLMResponse, MessageList, StopReason, ToolCall
from prodagent.llm.base import ChunkCallback, LLMConfig, normalise_content
from prodagent.resilience.transport.http_retry import with_http_retry

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

logger = logging.getLogger(__name__)


class AnthropicAdapter:
    """Wraps the Anthropic SDK with prompt caching, streaming, and retry."""

    def __init__(
        self,
        api_key: str | None = None,
        default_config: LLMConfig | None = None,
        *,
        base_url: str | None = None,
    ) -> None:
        import os

        actual_base_url = base_url or os.getenv("ANTHROPIC_BASE_URL")
        resolved_key = api_key or os.getenv("ANTHROPIC_API_KEY", "")

        client_kwargs: dict[str, Any] = {}
        if actual_base_url:
            client_kwargs["base_url"] = actual_base_url
            logger.info("AnthropicAdapter: custom base_url=%s", actual_base_url)

        if resolved_key and not resolved_key.startswith("sk-ant-"):
            client_kwargs["auth_token"] = resolved_key
        else:
            client_kwargs["api_key"] = resolved_key or "dummy"

        self._client = anthropic.AsyncAnthropic(**client_kwargs)
        self._default_config = default_config or LLMConfig()

    async def complete(
        self,
        messages: MessageList,
        *,
        system: str | list[dict[str, Any]] = "",
        tools: list[dict[str, Any]] | None = None,
        config: LLMConfig | None = None,
        on_chunk: ChunkCallback | None = None,
    ) -> LLMResponse:
        cfg = config or self._default_config
        normalised = cast(
            "MessageList",
            self._normalise_messages(messages, cache_boundary_index=cfg.cache_boundary_index),
        )
        return await with_http_retry(
            lambda: self._stream(normalised, system=system, tools=tools, cfg=cfg, on_chunk=on_chunk)
        )

    @staticmethod
    def _tag_cache_boundary(content: Any) -> Any:
        """Mark the last content block as an ephemeral cache breakpoint."""
        if isinstance(content, list):
            blocks = [
                dict(b) if isinstance(b, dict) else {"type": "text", "text": str(b)}
                for b in content
            ]
            if blocks:
                blocks[-1] = {**blocks[-1], "cache_control": {"type": "ephemeral"}}
            return blocks
        text = content if isinstance(content, str) else str(content)
        return [{"type": "text", "text": text, "cache_control": {"type": "ephemeral"}}]

    def _normalise_messages(
        self, messages: MessageList, *, cache_boundary_index: int | None = None
    ) -> list[dict[str, Any]]:
        import json as _json

        result: list[dict[str, Any]] = []
        pending_tool_results: list[dict[str, Any]] = []

        def _flush_tool_results() -> None:
            if pending_tool_results:
                result.append({"role": "user", "content": list(pending_tool_results)})
                pending_tool_results.clear()

        for i, msg in enumerate(messages):
            role = msg.get("role")
            content = msg.get("content")
            at_boundary = i == cache_boundary_index

            if role == "tool":
                pending_tool_results.append(
                    {
                        "type": "tool_result",
                        "tool_use_id": msg.get("tool_call_id", ""),
                        "content": str(content),
                    }
                )
                if at_boundary:
                    pending_tool_results[-1] = {
                        **pending_tool_results[-1],
                        "cache_control": {"type": "ephemeral"},
                    }
                continue

            # Non-tool message ends the current batch of tool results
            _flush_tool_results()

            if role == "assistant" and msg.get("tool_calls"):
                content_blocks: list[dict[str, Any]] = []
                if content:
                    content_blocks.append({"type": "text", "text": content})
                for tc in msg["tool_calls"]:
                    raw_args = tc["function"]["arguments"]
                    try:
                        parsed_input = _json.loads(raw_args)
                    except _json.JSONDecodeError as exc:
                        raise ToolCallParseError(
                            f"Anthropic tool call {tc['function']['name']!r} had non-JSON arguments",
                            tool_name=tc["function"]["name"],
                            args_fragment=str(raw_args)[:200],
                        ) from exc
                    content_blocks.append(
                        {
                            "type": "tool_use",
                            "id": tc["id"],
                            "name": tc["function"]["name"],
                            "input": parsed_input,
                        }
                    )
                if at_boundary and content_blocks:
                    content_blocks[-1] = {
                        **content_blocks[-1],
                        "cache_control": {"type": "ephemeral"},
                    }
                result.append({"role": "assistant", "content": content_blocks})
                continue

            normalised_content = normalise_content(content)
            if at_boundary:
                normalised_content = self._tag_cache_boundary(normalised_content)
            result.append(
                {**msg, "content": normalised_content}
                if normalised_content is not content
                else dict(msg)
            )

        _flush_tool_results()
        return result

    def _build_system(self, system: str | list[dict[str, Any]], cfg: LLMConfig) -> Any:
        if not system:
            return None
        if isinstance(system, str):
            if not cfg.enable_prompt_caching:
                return system
            return [{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}]
        # Shallow-copy so we don't mutate caller's dicts, then tag the last block.
        blocks = [dict(b) for b in system]
        if cfg.enable_prompt_caching and blocks:
            blocks[-1] = {**blocks[-1], "cache_control": {"type": "ephemeral"}}
        return blocks

    def _build_kwargs(
        self,
        messages: MessageList,
        system: str | list[dict[str, Any]],
        tools: list[dict[str, Any]] | None,
        cfg: LLMConfig,
    ) -> dict[str, Any]:
        kwargs: dict[str, Any] = {
            "model": cfg.model,
            "max_tokens": cfg.max_tokens,
            "temperature": cfg.temperature,
            "messages": messages,
        }
        if cfg.timeout_seconds is not None:
            kwargs["timeout"] = cfg.timeout_seconds
        sys_val = self._build_system(system, cfg)
        if sys_val is not None:
            kwargs["system"] = sys_val
        if tools:
            # Tag the last tool so the (stable) tool defs join the cacheable prefix.
            if cfg.enable_prompt_caching:
                tools = [dict(t) for t in tools]
                tools[-1] = {**tools[-1], "cache_control": {"type": "ephemeral"}}
            kwargs["tools"] = tools
        return kwargs

    async def _stream(
        self,
        messages: MessageList,
        *,
        system: str | list[dict[str, Any]],
        tools: list[dict[str, Any]] | None,
        cfg: LLMConfig,
        on_chunk: Callable[[str], Awaitable[None]] | None,
    ) -> LLMResponse:
        kwargs = self._build_kwargs(messages, system, tools, cfg)
        async with self._client.messages.stream(**kwargs) as stream:
            async for text in stream.text_stream:
                if on_chunk is not None:
                    await on_chunk(text)
            raw = await stream.get_final_message()
        return self._parse_message(raw)

    @staticmethod
    def _parse_message(raw: Any) -> LLMResponse:
        tool_calls: list[ToolCall] = []
        text_parts: list[str] = []
        thinking_parts: list[str] = []

        for block in raw.content:
            if block.type == "text":
                text_parts.append(block.text)
            elif block.type == "thinking":
                thinking_parts.append(getattr(block, "thinking", "") or "")
            elif block.type == "tool_use":
                tool_calls.append(
                    ToolCall(
                        name=block.name,
                        params=dict(block.input),
                        call_id=block.id,
                    )
                )

        usage = raw.usage
        return LLMResponse(
            content="\n".join(text_parts),
            tool_calls=tool_calls,
            stop_reason=StopReason.coerce(raw.stop_reason),
            input_tokens=usage.input_tokens,
            output_tokens=usage.output_tokens,
            model=raw.model,
            cache_read_tokens=getattr(usage, "cache_read_input_tokens", 0) or 0,
            cache_write_tokens=getattr(usage, "cache_creation_input_tokens", 0) or 0,
            reasoning_content="\n".join(thinking_parts),
        )
