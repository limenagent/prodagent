from __future__ import annotations

from typing import TYPE_CHECKING

from prodagent import Agent, AgentConfig, ExecutionMode
from prodagent.core.config import FrameworkConfig
from prodagent.core.types import LLMResponse
from prodagent.llm.fake import FakeLLMAdapter

if TYPE_CHECKING:
    from pathlib import Path


def _child_that_fails_to_plan() -> Agent:
    llm = FakeLLMAdapter(
        responses=[LLMResponse(content="not valid json at all", stop_reason="end_turn")]
    )
    from prodagent import ExecutionMode

    return Agent(
        "broken_planner",
        system_prompt="plan something",
        mode=ExecutionMode.PLAN_FIRST,
        config=AgentConfig(
            name="broken_planner", llm=llm, description="A child whose planner fails"
        ),
    )


def _isolated_fw(tmp_path: Path) -> FrameworkConfig:
    """Isolated runs_dir so the test never loads a stale checkpoint from a
    prior run — the child lazy-resolves the checkpoint store from this config."""
    from dataclasses import replace as _dc_replace

    fw = FrameworkConfig.default()
    return _dc_replace(
        fw,
        orchestration=_dc_replace(
            fw.orchestration,
            runs_dir=str(tmp_path / "runs"),
            events_dir=str(tmp_path / "events"),
        ),
    )


async def test_run_child_directly_returns_failed_on_none_run(tmp_path: Path) -> None:
    from prodagent.coordination.parent_runtime import ParentRuntime
    from prodagent.coordination.spawn import Spawn

    child = _child_that_fails_to_plan()
    llm = FakeLLMAdapter(responses=[LLMResponse(content="not json", stop_reason="end_turn")])
    pipeline = Spawn(
        [child],
        llm=llm,
        hooks=None,
        framework_config=_isolated_fw(tmp_path),
        ctx=ParentRuntime(
            constraints=[],
            budget=None,
            parent_run_id="parent-1",
            checkpoint=None,
            event_log=None,
        ),
    )
    result = await pipeline.spawn("broken_planner", "do something")

    assert result["state"] == "failed"
    assert "no terminal result" in result["output"] or "failed" in result["output"].lower()


async def test_run_child_returns_failed_when_executor_raises(tmp_path: Path) -> None:
    from prodagent.coordination.parent_runtime import ParentRuntime
    from prodagent.coordination.spawn import Spawn

    class _BoomLLM:
        async def complete(self, messages, *, system="", tools=None, config=None, on_chunk):
            raise RuntimeError("simulated LLM explosion")

    child = Agent(
        "exploder",
        system_prompt="will crash",
        mode=ExecutionMode.REACTIVE,
        config=AgentConfig(
            name="exploder",
            llm=_BoomLLM(),  # type: ignore[arg-type]
            description="A child whose LLM raises",
        ),
    )
    pipeline = Spawn(
        [child],
        llm=_BoomLLM(),  # type: ignore[arg-type]
        hooks=None,
        framework_config=_isolated_fw(tmp_path),
        ctx=ParentRuntime(
            constraints=[],
            budget=None,
            parent_run_id="parent-2",
            checkpoint=None,
            event_log=None,
        ),
    )
    result = await pipeline.spawn("exploder", "do something")

    assert result["state"] == "failed"
    assert "simulated LLM explosion" in result["output"] or "failed" in result["output"].lower()
