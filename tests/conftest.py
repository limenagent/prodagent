import os
import tempfile
from pathlib import Path
from unittest.mock import AsyncMock

import pytest


@pytest.fixture(autouse=True)
def _isolate_default_dirs(monkeypatch, tmp_path):
    """Redirect prodagent's default file-based store directories into the
    test's isolated ``tmp_path`` so stale state from prior runs (or
    parallel tests) can't leak in.

    Before this fixture, every test that didn't explicitly wire a
    ``FrameworkConfig`` shared the same ``.prodagent/sessions/`` (etc.)
    on disk, which caused flaky run_id assertions and checkpoint-not-found
    failures whenever old session files lingered across test sessions.
    """
    from prodagent.core.config import FrameworkConfig

    _original_default = FrameworkConfig.default

    @staticmethod
    def _isolated_default() -> FrameworkConfig:
        cfg = _original_default()
        cfg.orchestration.sessions_dir = str(tmp_path / "sessions")
        cfg.orchestration.runs_dir = str(tmp_path / "runs")
        cfg.orchestration.events_dir = str(tmp_path / "events")
        cfg.orchestration.spans_path = str(tmp_path / "spans.jsonl")
        cfg.orchestration.experience_path = str(tmp_path / "experiences.jsonl")
        return cfg

    monkeypatch.setattr(FrameworkConfig, "default", _isolated_default)


@pytest.fixture(autouse=True)
def fake_llm_mode():
    original_value = os.environ.get("USE_FAKE_LLM")
    os.environ["USE_FAKE_LLM"] = "true"
    yield
    if original_value is None:
        os.environ.pop("USE_FAKE_LLM", None)
    else:
        os.environ["USE_FAKE_LLM"] = original_value


@pytest.fixture
def mock_llm_response():
    return {
        "id": "test-123",
        "type": "message",
        "role": "assistant",
        "content": """
{
  "tool_calls": [
    {
      "name": "check_status",
      "input": {
        "service": "test"
      }
    }
  ]
}
        """,
        "model": "claude-3-5-20250101",
        "stop_reason": "tool_use",
        "usage": {"input_tokens": 100, "output_tokens": 50},
    }


@pytest.fixture
def fake_llm(mock_llm_response):
    from prodagent.llm.fake import script

    return script(
        {
            "tools": [{"name": "check_status", "params": {"service": "test"}}],
            "content": "Status: OK",
        }
    )


@pytest.fixture
async def async_llm_client():
    client = AsyncMock()
    client.complete = AsyncMock()
    return client


@pytest.fixture
def sample_tool():
    from prodagent.tooling import tool

    @tool(name="test_tool", readonly=True)
    async def test_tool(param: str) -> dict:
        return {"result": param}

    return test_tool


@pytest.fixture
def hook_registry():
    from prodagent.kernel.bus import HookRegistry

    return HookRegistry()


@pytest.fixture
def simple_agent(fake_llm, hook_registry):
    from prodagent.runtime.agent import Agent
    from prodagent.runtime.config import AgentConfig

    return Agent(
        name="test_agent",
        system_prompt="Test goal",
        config=AgentConfig(name="test_agent", llm=fake_llm, hooks=hook_registry),
    )


@pytest.fixture
def sample_memory():
    from prodagent.cognition.memory import MemoryRecord, MemoryType

    return MemoryRecord(
        content="Test context content",
        memory_type=MemoryType.EPISODIC,
        domain="test",
    )


@pytest.fixture
def mock_async_context_manager():

    class MockAsyncContextManager:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

    return MockAsyncContextManager()


@pytest.fixture
async def temp_directory():

    with tempfile.TemporaryDirectory() as temp_dir:
        yield Path(temp_dir)


def pytest_configure(config):
    config.addinivalue_line(
        "markers", "slow: marks tests as slow (deselect with '-m \"not slow\"')"
    )
    config.addinivalue_line("markers", "integration: marks tests as integration tests")
    config.addinivalue_line("markers", "e2e: marks tests as end-to-end tests")
    config.addinivalue_line("markers", "requires_api: marks tests requiring real API keys")


@pytest.fixture
def sample_task():
    return "Check if system is healthy"


@pytest.fixture
def sample_incident():
    return {
        "incident_id": "INC-TEST-001",
        "title": "Test incident",
        "severity": "P2",
        "service": "test-service",
    }
