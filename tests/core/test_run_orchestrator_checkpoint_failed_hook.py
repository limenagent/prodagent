from __future__ import annotations

import pytest

from prodagent import Agent, AgentConfig, ExecutionMode
from prodagent.backends.file.checkpoint import FileCheckpointStore
from prodagent.kernel.bus import HookEvent, HookRegistry
from prodagent.llm.fake import script


@pytest.mark.asyncio
async def test_agent_run_fires_checkpoint_failed_once_across_both_save_sites(tmp_path, monkeypatch):
    import prodagent.backends.file.checkpoint as checkpoint_module

    def _boom(*_a, **_k):
        raise OSError("simulated disk full")

    monkeypatch.setattr(checkpoint_module, "write_atomic_json", _boom)

    hooks = HookRegistry()
    seen: list[dict] = []
    hooks.register_event(HookEvent.CHECKPOINT_FAILED, lambda **kw: seen.append(kw))

    agent = Agent(
        "checkpoint-fail-agent",
        system_prompt="Say hi",
        mode=ExecutionMode.REACTIVE,
        config=AgentConfig(
            name="checkpoint-fail-agent",
            llm=script({"content": "hi"}),
            hooks=hooks,
            checkpoint=FileCheckpointStore(tmp_path / "checkpoints"),
        ),
    )

    run = await agent.chat("hello", session_id="run-CF-orch")

    assert run.checkpoint_failed is True
    assert len(seen) == 1, f"CHECKPOINT_FAILED must fire exactly once, fired {len(seen)} times"
    assert seen[0]["run_id"] == "run-CF-orch:1"


@pytest.mark.asyncio
async def test_agent_run_never_fires_checkpoint_failed_when_healthy(tmp_path):
    hooks = HookRegistry()
    seen: list[dict] = []
    hooks.register_event(HookEvent.CHECKPOINT_FAILED, lambda **kw: seen.append(kw))

    agent = Agent(
        "checkpoint-ok-agent",
        system_prompt="Say hi",
        mode=ExecutionMode.REACTIVE,
        config=AgentConfig(
            name="checkpoint-ok-agent",
            llm=script({"content": "hi"}),
            hooks=hooks,
            checkpoint=FileCheckpointStore(tmp_path / "checkpoints"),
        ),
    )

    run = await agent.chat("hello", session_id="run-OK-orch")

    assert run.checkpoint_failed is False
    assert seen == []
