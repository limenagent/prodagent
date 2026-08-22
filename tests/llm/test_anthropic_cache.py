from __future__ import annotations

import pytest

from prodagent.llm import LLMConfig
from prodagent.llm.anthropic_adapter import AnthropicAdapter


@pytest.fixture
def adapter() -> AnthropicAdapter:
    return AnthropicAdapter(api_key="dummy", default_config=LLMConfig())


class TestBuildSystemCacheBreakpoint:
    def test_string_system_with_caching_returns_block_with_cache_control(self, adapter):
        cfg = LLMConfig(model="claude-sonnet-4-6", enable_prompt_caching=True)
        result = adapter._build_system("You are a triage agent.", cfg)
        assert isinstance(result, list)
        assert len(result) == 1
        assert result[0]["type"] == "text"
        assert result[0]["text"] == "You are a triage agent."
        assert result[0]["cache_control"] == {"type": "ephemeral"}

    def test_string_system_without_caching_returns_plain_string(self, adapter):
        cfg = LLMConfig(model="claude-sonnet-4-6", enable_prompt_caching=False)
        result = adapter._build_system("You are a triage agent.", cfg)
        assert result == "You are a triage agent."

    def test_empty_system_returns_none(self, adapter):
        cfg = LLMConfig(enable_prompt_caching=True)
        assert adapter._build_system("", cfg) is None
        assert adapter._build_system(None, cfg) is None

    def test_multiblock_system_tags_last_block_only(self, adapter):
        cfg = LLMConfig(enable_prompt_caching=True)
        blocks = [
            {"type": "text", "text": "Stable system prompt."},
            {"type": "text", "text": "Volatile per-turn context."},
        ]
        result = adapter._build_system(blocks, cfg)
        assert isinstance(result, list)
        assert len(result) == 2
        assert "cache_control" not in result[0]
        assert result[1]["cache_control"] == {"type": "ephemeral"}

    def test_multiblock_system_without_caching_no_cache_control(self, adapter):
        cfg = LLMConfig(enable_prompt_caching=False)
        blocks = [{"type": "text", "text": "block 1"}, {"type": "text", "text": "block 2"}]
        result = adapter._build_system(blocks, cfg)
        assert isinstance(result, list)
        assert all("cache_control" not in b for b in result)

    def test_build_system_does_not_mutate_caller_blocks(self, adapter):
        cfg = LLMConfig(enable_prompt_caching=True)
        original = [{"type": "text", "text": "hello"}]
        adapter._build_system(original, cfg)
        assert "cache_control" not in original[0]

    def test_no_model_allowlist_any_model_caches(self, adapter):
        cfg = LLMConfig(model="some-custom-model", enable_prompt_caching=True)
        result = adapter._build_system("prompt", cfg)
        assert isinstance(result, list)
        assert result[0]["cache_control"] == {"type": "ephemeral"}


class TestBuildKwargsToolCacheBreakpoint:
    def test_last_tool_gets_cache_control(self, adapter):
        cfg = LLMConfig(enable_prompt_caching=True)
        tools = [
            {"name": "tool_a", "description": "a", "input_schema": {}},
            {"name": "tool_b", "description": "b", "input_schema": {}},
        ]
        kwargs = adapter._build_kwargs(messages=[], system="sys", tools=tools, cfg=cfg)
        assert "cache_control" not in kwargs["tools"][0]
        assert kwargs["tools"][1]["cache_control"] == {"type": "ephemeral"}

    def test_no_cache_control_on_tools_when_disabled(self, adapter):
        cfg = LLMConfig(enable_prompt_caching=False)
        tools = [{"name": "tool_a", "description": "a", "input_schema": {}}]
        kwargs = adapter._build_kwargs(messages=[], system="sys", tools=tools, cfg=cfg)
        assert all("cache_control" not in t for t in kwargs["tools"])

    def test_build_kwargs_does_not_mutate_caller_tools(self, adapter):
        cfg = LLMConfig(enable_prompt_caching=True)
        tools = [{"name": "tool_a", "description": "a", "input_schema": {}}]
        adapter._build_kwargs(messages=[], system="sys", tools=tools, cfg=cfg)
        assert "cache_control" not in tools[0]

    def test_system_and_tools_both_cached(self, adapter):
        cfg = LLMConfig(enable_prompt_caching=True)
        tools = [{"name": "t", "description": "d", "input_schema": {}}]
        kwargs = adapter._build_kwargs(messages=[], system="sys", tools=tools, cfg=cfg)
        assert isinstance(kwargs["system"], list)
        assert kwargs["system"][-1]["cache_control"] == {"type": "ephemeral"}
        assert kwargs["tools"][-1]["cache_control"] == {"type": "ephemeral"}


class TestNormaliseMessagesCacheBoundary:
    def test_none_boundary_tags_nothing(self, adapter):
        messages = [
            {"role": "user", "content": "hello"},
            {"role": "assistant", "content": "hi there"},
        ]
        result = adapter._normalise_messages(messages, cache_boundary_index=None)
        assert all(not isinstance(m["content"], list) for m in result)

    def test_plain_text_message_gets_tagged_as_block(self, adapter):
        messages = [
            {"role": "user", "content": "hello"},
            {"role": "assistant", "content": "hi there"},
        ]
        result = adapter._normalise_messages(messages, cache_boundary_index=0)
        assert result[0]["content"] == [
            {"type": "text", "text": "hello", "cache_control": {"type": "ephemeral"}}
        ]
        assert result[1]["content"] == "hi there"

    def test_tool_result_batch_gets_tagged_on_matching_source_index(self, adapter):
        messages = [
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {
                        "id": "call_1",
                        "type": "function",
                        "function": {"name": "foo", "arguments": "{}"},
                    }
                ],
            },
            {"role": "tool", "tool_call_id": "call_1", "content": "result-1"},
            {"role": "user", "content": "next turn"},
        ]
        result = adapter._normalise_messages(messages, cache_boundary_index=1)
        tool_result_msg = next(
            m for m in result if m["role"] == "user" and isinstance(m["content"], list)
        )
        assert tool_result_msg["content"][-1]["cache_control"] == {"type": "ephemeral"}

    def test_assistant_with_tool_calls_gets_tagged_on_last_block(self, adapter):
        messages = [
            {
                "role": "assistant",
                "content": "thinking",
                "tool_calls": [
                    {
                        "id": "call_1",
                        "type": "function",
                        "function": {"name": "foo", "arguments": "{}"},
                    }
                ],
            },
        ]
        result = adapter._normalise_messages(messages, cache_boundary_index=0)
        assistant_msg = result[0]
        assert assistant_msg["content"][-1]["cache_control"] == {"type": "ephemeral"}
        assert assistant_msg["content"][-1]["type"] == "tool_use"

    def test_boundary_index_out_of_range_tags_nothing(self, adapter):
        messages = [{"role": "user", "content": "hello"}]
        result = adapter._normalise_messages(messages, cache_boundary_index=5)
        assert result[0]["content"] == "hello"


class TestOpenAIAdapterSystemNormalization:
    def test_string_system_passes_through(self):
        from prodagent.llm.openai_adapter import OpenAIAdapter

        adapter = OpenAIAdapter(api_key="dummy", model="gpt-4o")
        full = adapter._build_messages([], "You are helpful.")
        assert full[0] == {"role": "system", "content": "You are helpful."}

    def test_multiblock_system_concatenates_text_drops_cache_control(self):
        from prodagent.llm.openai_adapter import OpenAIAdapter

        adapter = OpenAIAdapter(api_key="dummy", model="gpt-4o")
        blocks = [
            {"type": "text", "text": "Stable.", "cache_control": {"type": "ephemeral"}},
            {"type": "text", "text": "Volatile."},
        ]
        full = adapter._build_messages([], blocks)
        assert full[0]["role"] == "system"
        assert "Stable." in full[0]["content"]
        assert "Volatile." in full[0]["content"]
        assert "cache_control" not in full[0]
