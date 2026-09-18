"""Wire normalization of the OpenAI adapter: exactly one leading system message,
no matter what the context strategy injected into the window."""

from src.kernel import ToolCall
from src.runtime.openai_lite import OpenAICompatibleLlm, _wire


def _llm() -> OpenAICompatibleLlm:
    # Explicit args keep the constructor off the environment; nothing here talks
    # to the network — _payload is a pure function.
    return OpenAICompatibleLlm(api_key="test", base_url="http://localhost")


def test_payload_folds_window_systems_into_the_leading_one():
    llm = _llm()
    messages = [
        {"role": "user", "content": "研究新能源赛道"},
        {"role": "system", "content": "[History summary] 市场规模、增速、主要玩家"},
        {"role": "assistant", "text": "报告……"},
    ]
    payload = llm._payload(messages, None, "你是行业研究员。")
    roles = [m["role"] for m in payload["messages"]]
    # one system, at the head, carrying both the instruction and the summary
    assert roles.count("system") == 1 and roles[0] == "system"
    assert "你是行业研究员。" in payload["messages"][0]["content"]
    assert "[History summary]" in payload["messages"][0]["content"]
    # the rest of the window passes through in order
    assert roles[1:] == ["user", "assistant"]


def test_payload_without_window_systems_is_untouched():
    llm = _llm()
    messages = [
        {"role": "user", "content": "q"},
        {"role": "assistant", "text": "a"},
    ]
    payload = llm._payload(messages, None, "sys")
    assert [m["role"] for m in payload["messages"]] == ["system", "user", "assistant"]
    assert payload["messages"][0]["content"] == "sys"


def _assistant_with_two_same_name_calls():
    return {
        "role": "assistant",
        "tool_calls": [
            ToolCall("search", {"q": "a"}, "id_a"),
            ToolCall("search", {"q": "b"}, "id_b"),
        ],
    }


def test_wire_pairs_repeated_same_name_tool_results_by_id():
    # The ReAct recipe carries the model-assigned id on each tool message, so two
    # calls to the same tool in one round keep their ids instead of swapping.
    messages = [
        _assistant_with_two_same_name_calls(),
        {"role": "tool", "name": "search", "tool_call_id": "id_a", "content": "A"},
        {"role": "tool", "name": "search", "tool_call_id": "id_b", "content": "B"},
    ]
    wired = [m for m in _wire(messages) if m["role"] == "tool"]
    assert [(m["tool_call_id"], m["content"]) for m in wired] == [
        ("id_a", "A"),
        ("id_b", "B"),
    ]


def test_wire_legacy_tool_messages_pair_in_declaration_order():
    # Scripted/legacy tool messages without an id fall back to declaration order,
    # which is still correct for repeated same-name calls (no id collapse).
    messages = [
        _assistant_with_two_same_name_calls(),
        {"role": "tool", "name": "search", "tool_call_id": "", "content": "A"},
        {"role": "tool", "name": "search", "tool_call_id": "", "content": "B"},
    ]
    wired = [m for m in _wire(messages) if m["role"] == "tool"]
    assert [m["tool_call_id"] for m in wired] == ["id_a", "id_b"]
