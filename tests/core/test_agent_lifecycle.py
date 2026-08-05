import tempfile
from dataclasses import replace as _dc_replace
from pathlib import Path

import pytest

from prodagent import Agent, ExecutionMode, HardBudget
from prodagent.core.config import FrameworkConfig
from prodagent.hooks.registry import HookEvent, HookRegistry
from prodagent.llm.fake import script
from prodagent.tooling import tool


@tool(name="echo")
async def echo_tool(text: str) -> str:
    return f"Echoed: {text}"


def _fw(tmpdir: str) -> FrameworkConfig:
    base = FrameworkConfig.default()
    return _dc_replace(
        base,
        orchestration=_dc_replace(
            base.orchestration,
            runs_dir=str(Path(tmpdir) / "checkpoints"),
            sessions_dir=str(Path(tmpdir) / "sessions"),
        ),
    )


@pytest.mark.asyncio
async def test_agent_lifecycle_full_roundtrip():
    with tempfile.TemporaryDirectory() as tmpdir:
        session_id = "lifecycle-test-001"

        agent = Agent(
            name="lifecycle-agent",
            system_prompt="Echo the input",
            tools=[echo_tool],
            budget=HardBudget(max_turns=2),
            llm=script({"content": "Echoed: hello world"}),
            hooks=HookRegistry(),
            framework=_fw(tmpdir),
            mode=ExecutionMode.REACTIVE,
        )

        run = await agent.chat("hello世界", session_id=session_id)
        assert run is not None
        assert run.state.value in ("completed", "running")
        assert run.run_id == f"{session_id}:1"

        assert (Path(tmpdir) / "checkpoints" / f"{session_id}:1.v1.json").exists()


@pytest.mark.asyncio
async def test_agent_lifecycle_with_early_termination():
    with tempfile.TemporaryDirectory() as tmpdir:
        session_id = "lifecycle-test-early-term"

        @tool(name="increment")
        async def increment_tool(counter: int) -> int:
            return counter + 1

        agent = Agent(
            name="early-term-agent",
            system_prompt="Count up",
            tools=[increment_tool],
            budget=HardBudget(max_turns=1),
            llm=script({"content": "Calling increment"}),
            hooks=HookRegistry(),
            framework=_fw(tmpdir),
            mode=ExecutionMode.REACTIVE,
        )

        run = await agent.chat("Start counting", session_id=session_id)

        assert (Path(tmpdir) / "checkpoints" / f"{session_id}:1.v1.json").exists()
        assert run.state.value in ("failed", "suspended", "completed")


@pytest.mark.asyncio
async def test_agent_lifecycle_with_hooks():
    with tempfile.TemporaryDirectory() as tmpdir:
        events = []

        hooks = HookRegistry()

        def on_session_start(**kwargs):
            events.append("session.start")

        def on_turn_start(**kwargs):
            events.append("turn.start")

        def on_tool_call(**kwargs):
            events.append("tool_call")

        def on_tool_result(**kwargs):
            events.append("tool_result")

        def on_session_end(**kwargs):
            events.append("session.end")

        hooks.register_event(HookEvent.SESSION_START, on_session_start)
        hooks.register_event(HookEvent.TURN_START, on_turn_start)
        hooks.register_event(HookEvent.TOOL_CALL, on_tool_call)
        hooks.register_event(HookEvent.TOOL_RESULT, on_tool_result)
        hooks.register_event(HookEvent.SESSION_END, on_session_end)

        agent = Agent(
            name="hooks-agent",
            system_prompt="Echo input",
            tools=[echo_tool],
            budget=HardBudget(max_turns=2),
            llm=script({"content": "Echoed: test"}),
            hooks=hooks,
            framework=_fw(tmpdir),
            mode=ExecutionMode.REACTIVE,
        )

        await agent.chat("test", session_id="hooks-1")

        assert "session.start" in events
        assert "session.end" in events


@pytest.mark.asyncio
async def test_agent_lifecycle_crash_recovery():
    with tempfile.TemporaryDirectory() as tmpdir:
        session_id = "crash-recovery-test"

        agent = Agent(
            name="crash-agent",
            system_prompt="Echo input",
            tools=[echo_tool],
            budget=HardBudget(max_turns=2),
            llm=script({"content": "Echoed: before crash"}),
            hooks=HookRegistry(),
            framework=_fw(tmpdir),
            mode=ExecutionMode.REACTIVE,
        )

        run1 = await agent.chat("before crash", session_id=session_id)

        assert (Path(tmpdir) / "checkpoints" / f"{session_id}:1.v1.json").exists()

        # A fresh agent instance loading the same session can re-chat (new turn).
        recovery_agent = Agent(
            name="recovery-agent",
            system_prompt="Echo input",
            tools=[echo_tool],
            budget=HardBudget(max_turns=2),
            llm=script({"content": "Echoed: after recovery"}),
            hooks=HookRegistry(),
            framework=_fw(tmpdir),
            mode=ExecutionMode.REACTIVE,
        )

        run2 = await recovery_agent.chat("continue", session_id=session_id)

        assert run2.run_id == f"{session_id}:2"
        assert run1.run_id == f"{session_id}:1"
