import tempfile
from dataclasses import replace as _dc_replace
from pathlib import Path

import pytest

from prodagent import Agent, AgentConfig, ExecutionMode, HardBudget
from prodagent.core.config import FrameworkConfig
from prodagent.kernel.bus import HookEvent, HookRegistry
from prodagent.llm.fake import script
from prodagent.tooling import tool


@tool(name="echo")
async def echo_tool(text: str) -> str:
    return f"Echoed: {text}"


def _fw(tmpdir: str) -> FrameworkConfig:
    from prodagent.core.config import production

    base = production(FrameworkConfig.default())
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
            "lifecycle-agent",
            system_prompt="Echo the input",
            tools=[echo_tool],
            budget=HardBudget(max_turns=2),
            mode=ExecutionMode.REACTIVE,
            config=AgentConfig(
                name="lifecycle-agent",
                llm=script({"content": "Echoed: hello world"}),
                hooks=HookRegistry(),
                framework=_fw(tmpdir),
            ),
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
            "early-term-agent",
            system_prompt="Count up",
            tools=[increment_tool],
            budget=HardBudget(max_turns=1),
            mode=ExecutionMode.REACTIVE,
            config=AgentConfig(
                name="early-term-agent",
                llm=script({"content": "Calling increment"}),
                hooks=HookRegistry(),
                framework=_fw(tmpdir),
            ),
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
            "hooks-agent",
            system_prompt="Echo input",
            tools=[echo_tool],
            budget=HardBudget(max_turns=2),
            mode=ExecutionMode.REACTIVE,
            config=AgentConfig(
                name="hooks-agent",
                llm=script({"content": "Echoed: test"}),
                hooks=hooks,
                framework=_fw(tmpdir),
            ),
        )

        await agent.chat("test", session_id="hooks-1")

        assert "session.start" in events
        assert "session.end" in events


@pytest.mark.asyncio
async def test_agent_lifecycle_crash_recovery():
    with tempfile.TemporaryDirectory() as tmpdir:
        session_id = "crash-recovery-test"

        agent = Agent(
            "crash-agent",
            system_prompt="Echo input",
            tools=[echo_tool],
            budget=HardBudget(max_turns=2),
            mode=ExecutionMode.REACTIVE,
            config=AgentConfig(
                name="crash-agent",
                llm=script({"content": "Echoed: before crash"}),
                hooks=HookRegistry(),
                framework=_fw(tmpdir),
            ),
        )

        run1 = await agent.chat("before crash", session_id=session_id)

        assert (Path(tmpdir) / "checkpoints" / f"{session_id}:1.v1.json").exists()

        # A fresh agent instance loading the same session can re-chat (new turn).
        recovery_agent = Agent(
            "recovery-agent",
            system_prompt="Echo input",
            tools=[echo_tool],
            budget=HardBudget(max_turns=2),
            mode=ExecutionMode.REACTIVE,
            config=AgentConfig(
                name="recovery-agent",
                llm=script({"content": "Echoed: after recovery"}),
                hooks=HookRegistry(),
                framework=_fw(tmpdir),
            ),
        )

        run2 = await recovery_agent.chat("continue", session_id=session_id)

        assert run2.run_id == f"{session_id}:2"
        assert run1.run_id == f"{session_id}:1"
