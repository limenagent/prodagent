from __future__ import annotations

import pytest

from prodagent.llm import create_llm_client
from prodagent.llm.fake import FakeLLMAdapter

_LLM_ENV_KEYS = [
    "USE_FAKE_LLM",
    "LLM_BASE_URL",
    "LLM_API_KEY",
    "LLM_MODEL",
    "ANTHROPIC_API_KEY",
    "ANTHROPIC_MODEL",
    "ANTHROPIC_BASE_URL",
]


@pytest.fixture(autouse=True)
def _clean_llm_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for key in _LLM_ENV_KEYS:
        monkeypatch.delenv(key, raising=False)


class TestFakeLLMShortCircuit:
    def test_use_fake_llm_env_returns_fake_adapter(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("USE_FAKE_LLM", "true")
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-real")
        client = create_llm_client()
        assert isinstance(client, FakeLLMAdapter)

    def test_use_fake_llm_env_value_yes_returns_fake(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("USE_FAKE_LLM", "yes")
        client = create_llm_client()
        assert isinstance(client, FakeLLMAdapter)

    def test_use_fake_llm_env_value_1_returns_fake(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("USE_FAKE_LLM", "1")
        client = create_llm_client()
        assert isinstance(client, FakeLLMAdapter)

    def test_force_fake_overrides_anthropic_key(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-real")
        client = create_llm_client(force_fake=True)
        assert isinstance(client, FakeLLMAdapter)

    def test_force_fake_overrides_openai_compat(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("LLM_BASE_URL", "https://api.deepseek.com/v1")
        monkeypatch.setenv("LLM_API_KEY", "ds-real")
        client = create_llm_client(force_fake=True)
        assert isinstance(client, FakeLLMAdapter)

    def test_no_env_vars_falls_back_to_fake(self) -> None:
        client = create_llm_client()
        assert isinstance(client, FakeLLMAdapter)


class TestOpenAICompatRouting:
    def test_llm_base_url_routes_to_openai_adapter(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from prodagent.llm.openai_adapter import OpenAIAdapter

        monkeypatch.setenv("LLM_BASE_URL", "https://api.deepseek.com/v1")
        monkeypatch.setenv("LLM_API_KEY", "ds-real")
        monkeypatch.setenv("LLM_MODEL", "deepseek-chat")
        client = create_llm_client()
        assert isinstance(client, OpenAIAdapter)
        assert str(client._client.base_url).rstrip("/") == "https://api.deepseek.com/v1"
        assert client._default_config.model == "deepseek-chat"
        assert client._client.api_key == "ds-real"

    def test_missing_api_key_uses_dummy(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from prodagent.llm.openai_adapter import OpenAIAdapter

        monkeypatch.setenv("LLM_BASE_URL", "http://localhost:11434/v1")
        monkeypatch.setenv("LLM_MODEL", "qwen2.5:32b")
        client = create_llm_client()
        assert isinstance(client, OpenAIAdapter)
        assert client._client.api_key == "dummy"

    def test_missing_model_defaults_to_gpt4o(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from prodagent.llm.openai_adapter import OpenAIAdapter

        monkeypatch.setenv("LLM_BASE_URL", "https://api.example.com/v1")
        monkeypatch.setenv("LLM_API_KEY", "k")
        client = create_llm_client()
        assert isinstance(client, OpenAIAdapter)
        assert client._default_config.model == "gpt-4o"

    def test_openai_compat_takes_priority_over_anthropic(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from prodagent.llm.openai_adapter import OpenAIAdapter

        monkeypatch.setenv("LLM_BASE_URL", "https://api.deepseek.com/v1")
        monkeypatch.setenv("LLM_API_KEY", "ds-real")
        monkeypatch.setenv("LLM_MODEL", "deepseek-chat")
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-real")
        client = create_llm_client()
        assert isinstance(client, OpenAIAdapter)


class TestAnthropicRouting:
    def test_anthropic_key_routes_to_anthropic_adapter(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from prodagent.llm.anthropic_adapter import AnthropicAdapter

        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-real")
        client = create_llm_client()
        assert isinstance(client, AnthropicAdapter)

    def test_anthropic_adapter_picks_up_explicit_model_env(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from prodagent.llm.anthropic_adapter import AnthropicAdapter

        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-real")
        monkeypatch.setenv("ANTHROPIC_MODEL", "claude-opus-4-7")
        client = create_llm_client()
        assert isinstance(client, AnthropicAdapter)
        assert client._default_config.model == "claude-opus-4-7"

    def test_anthropic_adapter_picks_up_base_url_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from prodagent.llm.anthropic_adapter import AnthropicAdapter

        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-real")
        monkeypatch.setenv("ANTHROPIC_BASE_URL", "https://internal.gateway/v1")
        client = create_llm_client()
        assert isinstance(client, AnthropicAdapter)
        assert str(client._client.base_url).rstrip("/") == "https://internal.gateway/v1"

    def test_anthropic_native_key_uses_api_key_header(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from prodagent.llm.anthropic_adapter import AnthropicAdapter

        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-real")
        client = create_llm_client()
        assert isinstance(client, AnthropicAdapter)
        assert client._client.api_key == "sk-ant-real"
        assert client._client.auth_token is None

    def test_anthropic_non_native_key_uses_auth_token_bearer(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from prodagent.llm.anthropic_adapter import AnthropicAdapter

        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        adapter = AnthropicAdapter(
            api_key="gateway-token-abc123", base_url="https://internal.gateway/v1"
        )
        assert adapter._client.auth_token == "gateway-token-abc123"
        assert adapter._client.api_key is None
