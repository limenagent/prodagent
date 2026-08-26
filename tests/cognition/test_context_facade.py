from unittest.mock import AsyncMock

import pytest

from prodagent.base.config import ContextConfig
from prodagent.cognition.context.manager import ContextManager


def _make_run(messages=None, task="test task"):
    run = AsyncMock()
    run.task = task
    run.turn_count = 1
    run.tool_failures = 0
    run.last_action = None
    run.state.value = "running"
    run.messages = messages or [{"role": "user", "content": "hello"}]
    return run


class TestContextManagerInitSignature:
    def test_accepts_phase_py_kwargs(self):
        cm = ContextManager(
            config=ContextConfig(max_tokens=10_000),
            system_prompt="you are an agent",
            constraint_reminder="- never delete data",
            llm=None,
        )
        assert cm is not None

    def test_defaults_when_no_config(self):
        cm = ContextManager()
        assert cm._max == ContextConfig().max_tokens


class TestPrepareReturnShape:
    @pytest.mark.asyncio
    async def test_prepare_returns_2_tuple(self):
        cm = ContextManager(
            config=ContextConfig(max_tokens=10_000),
            system_prompt="you are an agent",
        )
        run = _make_run()
        result = await cm.prepare(run)
        assert isinstance(result, tuple)
        assert len(result) == 2
        system, messages = result
        assert system == "you are an agent"
        assert isinstance(messages, list)
