"""Wire normalization of the OpenAI adapter: exactly one leading system message,
no matter what the context strategy injected into the window."""

from src.runtime.openai_lite import OpenAICompatibleLlm


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
