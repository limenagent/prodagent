"""Extended thinking — Anthropic adapter + kernel round-trip.

The chain: ``LLMConfig.thinking_budget_tokens`` enables it → the adapter
sends ``thinking``, stops sending ``temperature`` (API pins it to 1), and
keeps ``max_tokens`` above the budget → ``_parse_message`` captures the raw
blocks (signature included) onto ``LLMResponse.thinking_blocks`` →
``Step._account`` parks them on the assistant message → the next
``_normalise_messages`` re-sends them on tool-use continuations, which the
API requires. OpenAI's adapter strips the key instead — it's framework
vocabulary, not wire.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from prodagent.kernel.budget import HardBudget
from prodagent.kernel.state import AgentRun
from prodagent.kernel.step import Step
from prodagent.kernel.types import LLMResponse, ToolCall
from prodagent.llm import LLMConfig
from prodagent.llm.anthropic_adapter import AnthropicAdapter


@pytest.fixture
def adapter() -> AnthropicAdapter:
    return AnthropicAdapter(api_key="dummy", default_config=LLMConfig())


# ------------------------------------------------------------------ kwargs


def test_thinking_off_by_default_sends_temperature(adapter):
    cfg = LLMConfig(model="claude-sonnet-4-6", temperature=0.2)

    kwargs = adapter._build_kwargs([], "", None, cfg)

    assert "thinking" not in kwargs
    assert kwargs["temperature"] == 0.2


def test_thinking_budget_enables_param_and_omits_temperature(adapter):
    cfg = LLMConfig(model="claude-sonnet-4-6", thinking_budget_tokens=2048, temperature=0.2)

    kwargs = adapter._build_kwargs([], "", None, cfg)

    assert kwargs["thinking"] == {"type": "enabled", "budget_tokens": 2048}
    assert "temperature" not in kwargs  # the API pins it to 1 while thinking


def test_max_tokens_bumped_above_the_thinking_budget(adapter):
    cfg = LLMConfig(model="claude-sonnet-4-6", max_tokens=1024, thinking_budget_tokens=4096)

    kwargs = adapter._build_kwargs([], "", None, cfg)

    assert kwargs["max_tokens"] == 4096 + 1024  # API requires max_tokens > budget

    sane = LLMConfig(model="claude-sonnet-4-6", max_tokens=8192, thinking_budget_tokens=2048)
    assert adapter._build_kwargs([], "", None, sane)["max_tokens"] == 8192


# ------------------------------------------------------------- parse → blocks


def _raw_with_thinking() -> SimpleNamespace:
    return SimpleNamespace(
        content=[
            SimpleNamespace(type="thinking", thinking="hm, odd", signature="sig-1"),
            SimpleNamespace(type="text", text="Answer: 42"),
            SimpleNamespace(type="tool_use", id="tc-1", name="search", input={"q": "42"}),
        ],
        stop_reason="tool_use",
        usage=SimpleNamespace(
            input_tokens=10,
            output_tokens=5,
            cache_read_input_tokens=0,
            cache_creation_input_tokens=0,
        ),
        model="claude-sonnet-4-6",
    )


def test_parse_message_captures_thinking_blocks_with_signature():
    response = AnthropicAdapter._parse_message(_raw_with_thinking())

    assert response.reasoning_content == "hm, odd"
    assert response.thinking_blocks == [
        {"type": "thinking", "thinking": "hm, odd", "signature": "sig-1"}
    ]
    assert [tc.name for tc in response.tool_calls] == ["search"]


def test_thinking_blocks_serialize_round_trip():
    response = AnthropicAdapter._parse_message(_raw_with_thinking())

    restored = LLMResponse.from_dict(response.to_dict())

    assert restored.thinking_blocks == response.thinking_blocks
    assert restored.reasoning_content == "hm, odd"


# ------------------------------------------------- kernel parks blocks on msg


class _NoopRunner:
    async def run_batch(self, run, calls):  # pragma: no cover - never reached
        return
        yield


async def test_step_account_parks_thinking_blocks_on_assistant_message():
    llm_response = LLMResponse(
        content="",
        tool_calls=[ToolCall(name="search", params={"q": "42"}, call_id="tc-1")],
        stop_reason="tool_use",
        thinking_blocks=[{"type": "thinking", "thinking": "hm", "signature": "s"}],
        reasoning_content="hm",
    )

    class _OneShotLLM:
        async def complete(self, messages, *, system="", tools=None, config=None, on_chunk=None):
            return llm_response

    run = AgentRun(run_id="r", task="t")
    step = Step(_OneShotLLM(), _NoopRunner(), budget=HardBudget(max_turns=5))

    async for _ in step.run(run, system="s", tools=None):
        pass

    assistant = run.messages[-1]
    assert assistant["role"] == "assistant"
    assert assistant["thinking"] == [{"type": "thinking", "thinking": "hm", "signature": "s"}]
    assert assistant["tool_calls"][0]["function"]["name"] == "search"


# ---------------------------------------------------------- wire round-trip


def _history() -> list:
    return [
        {"role": "user", "content": "q"},
        {
            "role": "assistant",
            "content": "",
            "thinking": [{"type": "thinking", "thinking": "hm", "signature": "s"}],
            "tool_calls": [
                {
                    "id": "tc-1",
                    "type": "function",
                    "function": {"name": "search", "arguments": '{"q": "42"}'},
                }
            ],
        },
        {"role": "tool", "tool_call_id": "tc-1", "content": "result"},
    ]


def test_tool_use_continuation_resends_thinking_blocks_first(adapter):
    wire = adapter._normalise_messages(_history(), include_thinking=True)

    assistant = wire[1]
    assert assistant["role"] == "assistant"
    assert assistant["content"][0] == {"type": "thinking", "thinking": "hm", "signature": "s"}
    assert assistant["content"][1]["type"] == "tool_use"


def test_thinking_off_never_sends_stale_blocks(adapter):
    wire = adapter._normalise_messages(_history(), include_thinking=False)

    assert all(b["type"] != "thinking" for b in wire[1]["content"])
    # And the framework key never leaks as a wire field on any message.
    assert all("thinking" not in msg for msg in wire)


def test_plain_assistant_message_strips_the_framework_key(adapter):
    history = [
        {"role": "user", "content": "q"},
        {
            "role": "assistant",
            "content": "done",
            "thinking": [{"type": "thinking", "thinking": "x", "signature": "y"}],
        },
    ]

    wire = adapter._normalise_messages(history)

    assert wire[1] == {"role": "assistant", "content": "done"}


# ------------------------------------------------------------- OpenAI strips


def test_openai_build_messages_strips_the_thinking_key():
    from prodagent.llm.openai_adapter import OpenAIAdapter

    adapter = OpenAIAdapter(api_key="dummy")
    wire = adapter._build_messages(_history(), "sys")

    assert all("thinking" not in msg for msg in wire)
    # wire = [system, user, assistant-tool-turn, tool] — everything else passes through.
    assert wire[2]["role"] == "assistant"
    assert wire[2]["tool_calls"][0]["function"]["name"] == "search"
