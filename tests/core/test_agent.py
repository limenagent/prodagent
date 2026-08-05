from __future__ import annotations

import asyncio
import tempfile
from pathlib import Path

import pytest

from prodagent import Agent, ExecutionMode, HardBudget, RunState
from prodagent.hooks.registry import HookRegistry
from prodagent.llm.fake import script
from prodagent.tooling import tool


def _simple_agent(llm, **kwargs) -> Agent:
    return Agent(
        name="test-agent",
        system_prompt="Verify system status.",
        llm=llm,
        hooks=HookRegistry(),
        mode=ExecutionMode.REACTIVE,
        **kwargs,
    )


def test_agent_creation():
    llm = script({"content": "all good"})
    agent = _simple_agent(llm)
    assert agent.name == "test-agent"
    assert agent.mode == ExecutionMode.REACTIVE


def test_agent_run_completes():
    llm = script({"content": "Task completed successfully."})
    agent = _simple_agent(llm)
    run = asyncio.run(agent.chat("check system health"))
    assert run.state == RunState.COMPLETED
    assert run.final_output is not None


def test_agent_run_returns_agent_run():
    from prodagent.core.state import AgentRun

    llm = script({"content": "done"})
    agent = _simple_agent(llm)
    run = asyncio.run(agent.chat("any task"))
    assert isinstance(run, AgentRun)


def test_agent_with_tools():
    @tool(name="health_check", readonly=True)
    async def health_check(service: str) -> dict:
        return {"healthy": True, "service": service}

    llm = script(
        {"tool": "health_check", "params": {"service": "api"}},
        {"content": "Service is healthy."},
    )
    agent = Agent(
        name="ops",
        system_prompt="Run health check.",
        tools=[health_check],
        llm=llm,
        hooks=HookRegistry(),
        mode=ExecutionMode.REACTIVE,
    )
    run = asyncio.run(agent.chat("Is the API healthy?"))
    assert run.state == RunState.COMPLETED
    assert any(tc.name == "health_check" for tc in run.tool_history)


def test_agent_constraints_in_system_prompt():
    captured_systems: list[str] = []

    from prodagent.core.types import LLMResponse
    from prodagent.llm.base import LLMClient

    class CaptureLLM(LLMClient):
        async def complete(self, messages, *, system="", tools=None, config=None, on_chunk):
            captured_systems.append(system)
            return LLMResponse(content="done", stop_reason="end_turn")

    agent = Agent(
        name="constrained",
        system_prompt="Check things.",
        constraints=["ALWAYS validate input", "NEVER skip logging"],
        llm=CaptureLLM(),
        hooks=HookRegistry(),
        mode=ExecutionMode.REACTIVE,
    )
    asyncio.run(agent.chat("check"))

    assert any("ALWAYS validate input" in s for s in captured_systems)
    assert any("NEVER skip logging" in s for s in captured_systems)


def test_agent_context_in_system_prompt():
    captured_systems: list[str] = []

    from prodagent.core.types import LLMResponse
    from prodagent.llm.base import LLMClient

    class CaptureLLM(LLMClient):
        async def complete(self, messages, *, system="", tools=None, config=None, on_chunk):
            captured_systems.append(system)
            return LLMResponse(content="done", stop_reason="end_turn")

    agent = Agent(
        name="ctx-agent",
        system_prompt="Incident INC-001: payment service down",
        llm=CaptureLLM(),
        hooks=HookRegistry(),
        mode=ExecutionMode.REACTIVE,
    )
    asyncio.run(agent.chat("handle incident"))

    assert any("INC-001" in s for s in captured_systems)


def test_fluent_api_inject():
    from prodagent.hooks.checkpoint import InjectionPoint

    agent = Agent(
        name="test-agent",
        llm=script({"content": "ok"}),
        injectors=[(InjectionPoint.CONTEXT_INJECTOR, lambda q: f"Context: {q}")],
    )
    assert len(agent.injectors) == 1


def test_extend_human_approval_registers_checker():
    from prodagent.guardrail.approval import ApprovalGate
    from prodagent.hooks.bundles.security import ApprovalHooks
    from prodagent.hooks.checkpoint import CheckPoint
    from prodagent.hooks.registry import HookRegistry

    hooks = HookRegistry()
    ApprovalHooks(gate=ApprovalGate()).attach(hooks)
    assert len(hooks._check_handlers[CheckPoint.APPROVAL_REQUEST]) == 1


def test_fluent_api_budget():
    agent = Agent(
        name="test-agent",
        llm=script({"content": "ok"}),
        budget=HardBudget(max_turns=10, max_cost_usd=0.5, max_seconds=120.0),
    )
    assert agent.budget_config is not None
    assert agent.budget_config.max_turns == 10


def test_fluent_api_reactive_plan_first():
    agent = Agent(
        name="test-agent",
        llm=script({"content": "ok"}),
        mode=ExecutionMode.PLAN_FIRST,
    )
    assert agent.mode == ExecutionMode.PLAN_FIRST
    agent2 = Agent(
        name="test-agent-2",
        llm=script({"content": "ok"}),
        mode=ExecutionMode.REACTIVE,
    )
    assert agent2.mode == ExecutionMode.REACTIVE


def test_agent_saves_to_session_dir():
    from dataclasses import replace as _dc_replace

    from prodagent.core.config import FrameworkConfig

    llm = script({"content": "saved run complete"})

    with tempfile.TemporaryDirectory() as tmpdir:
        fw = _dc_replace(
            FrameworkConfig.default(),
            orchestration=_dc_replace(
                FrameworkConfig.default().orchestration,
                runs_dir=str(Path(tmpdir) / "checkpoints"),
                sessions_dir=str(Path(tmpdir) / "sessions"),
            ),
        )
        agent = Agent(
            name="session-agent",
            system_prompt="Do some work.",
            llm=llm,
            hooks=HookRegistry(),
            framework=fw,
            mode=ExecutionMode.REACTIVE,
        )
        run = asyncio.run(agent.chat("save this run", session_id="run-save-001"))
        assert run.state == RunState.COMPLETED
        assert run.run_id == "run-save-001:1"

        assert (Path(tmpdir) / "checkpoints" / "run-save-001:1.v1.json").exists()


def test_agent_budget_respected():
    from prodagent.core.types import LLMResponse
    from prodagent.llm.base import LLMClient

    class InfiniteLoopLLM(LLMClient):
        async def complete(self, messages, *, system="", tools=None, config=None, on_chunk):
            return LLMResponse(content="thinking...", stop_reason="end_turn")

    agent = Agent(
        name="budget-test",
        system_prompt="Keep going.",
        llm=InfiniteLoopLLM(),
        hooks=HookRegistry(),
        budget=HardBudget(max_turns=2, max_seconds=30.0),
        mode=ExecutionMode.REACTIVE,
    )
    run = asyncio.run(agent.chat("test budget"))
    assert run.state in (RunState.COMPLETED, RunState.FAILED)
    assert run.turn_count <= 3


def test_agent_stream_yields_events():
    from prodagent.core.events import RunCompletedEvent
    from prodagent.tooling import tool as tool_decorator

    @tool_decorator(name="probe", readonly=True)
    async def probe(target: str) -> dict:
        return {"alive": True}

    llm = script(
        {"tool": "probe", "params": {"target": "db"}},
        {"content": "All systems operational."},
    )
    agent = Agent(
        name="stream-test",
        system_prompt="Check systems.",
        tools=[probe],
        llm=llm,
        hooks=HookRegistry(),
        mode=ExecutionMode.REACTIVE,
    )

    events = []

    async def drain():
        async for ev in agent.chat_stream("check all systems"):
            events.append(ev)

    asyncio.run(drain())

    types_seen = {type(e).__name__ for e in events}
    assert "RunCompletedEvent" in types_seen
    assert "ToolCallStartEvent" in types_seen
    assert "ToolResultEvent" in types_seen
    assert isinstance(events[-1], RunCompletedEvent)


def test_agent_stream_run_failed_event_on_budget_exhaustion():
    from prodagent.core.events import RunFailedEvent
    from prodagent.core.types import LLMResponse
    from prodagent.llm.base import LLMClient
    from prodagent.resilience.cost import HardBudget

    class LoopingLLM(LLMClient):
        async def complete(self, messages, *, system="", tools=None, config=None, on_chunk):
            return LLMResponse(content="still thinking...", stop_reason="end_turn")

    agent = Agent(
        name="budget-stream",
        system_prompt="Loop forever.",
        llm=LoopingLLM(),
        hooks=HookRegistry(),
        budget=HardBudget(max_turns=2, max_seconds=30.0),
        mode=ExecutionMode.REACTIVE,
    )

    events = []

    async def drain():
        async for ev in agent.chat_stream("run until budget"):
            events.append(ev)

    asyncio.run(drain())

    final = events[-1] if events else None
    assert final is not None
    from prodagent.core.events import RunCompletedEvent

    assert isinstance(final, RunCompletedEvent | RunFailedEvent)


@pytest.mark.asyncio
async def test_plan_first_failed_run_does_not_raise_attribute_error():
    from prodagent.core.events import RunFailedEvent
    from prodagent.core.types import LLMResponse
    from prodagent.llm.fake import FakeLLMAdapter

    bad_plan_llm = FakeLLMAdapter(
        responses=[LLMResponse(content="not valid json", stop_reason="end_turn")]
    )

    agent = Agent(
        name="plan-fail",
        system_prompt="Parse my plan.",
        llm=bad_plan_llm,
        hooks=HookRegistry(),
    )
    assert agent.mode is ExecutionMode.PLAN_FIRST

    events: list = []

    async for ev in agent.chat_stream("do something"):
        events.append(ev)

    final = events[-1] if events else None
    assert final is not None, "stream() must yield at least the terminal event"
    assert isinstance(final, RunFailedEvent), (
        f"expected RunFailedEvent from FAILED plan, got {type(final).__name__}: {final}"
    )
    assert final.run.state is RunState.FAILED


def test_agent_name_with_child_separator_rejected():
    """Agent name containing '::' is rejected at construction — it would
    collide with the parent::child run_id derivation."""
    with pytest.raises(ValueError, match="::"):
        Agent(name="bad::name", system_prompt="", llm=script({"content": "x"}))
