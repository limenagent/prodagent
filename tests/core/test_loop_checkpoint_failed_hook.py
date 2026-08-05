from __future__ import annotations

import pytest

from prodagent.backends.file.checkpoint import FileCheckpointStore
from prodagent.core.types import LLMResponse
from prodagent.hooks.events import HookEvent
from prodagent.hooks.registry import HookRegistry
from prodagent.llm.fake import FakeLLMAdapter
from prodagent.runtime.reactive import AgentLoop
from prodagent.tooling.dispatcher import ToolDispatcher


def _make_loop(llm, store, hooks):
    dispatcher = ToolDispatcher({})
    return AgentLoop(
        llm,
        dispatcher,
        system_prompt="test",
        tools_schema=[],
        checkpoint_store=store,
        hooks=hooks,
    )


@pytest.mark.asyncio
async def test_loop_fires_checkpoint_failed_when_write_fails(tmp_path, monkeypatch):
    import prodagent.backends.file.checkpoint as checkpoint_module

    def _boom(*_a, **_k):
        raise OSError("simulated disk full")

    monkeypatch.setattr(checkpoint_module, "write_atomic_json", _boom)

    store = FileCheckpointStore(directory=tmp_path)
    hooks = HookRegistry()
    seen: list[dict] = []
    hooks.register_event(HookEvent.CHECKPOINT_FAILED, lambda **kw: seen.append(kw))

    llm = FakeLLMAdapter(responses=[LLMResponse(content="done", stop_reason="end_turn")])
    loop = _make_loop(llm, store, hooks)

    streamed: list = []
    async for event in loop.stream("diagnose", run_id="run-CF"):
        streamed.append(event)

    assert len(seen) == 1
    assert seen[0]["run_id"] == "run-CF"


@pytest.mark.asyncio
async def test_loop_does_not_fire_checkpoint_failed_on_success(tmp_path):
    store = FileCheckpointStore(directory=tmp_path)
    hooks = HookRegistry()
    seen: list[dict] = []
    hooks.register_event(HookEvent.CHECKPOINT_FAILED, lambda **kw: seen.append(kw))

    llm = FakeLLMAdapter(responses=[LLMResponse(content="done", stop_reason="end_turn")])
    loop = _make_loop(llm, store, hooks)

    async for _ in loop.stream("diagnose", run_id="run-OK"):
        pass

    assert seen == []
