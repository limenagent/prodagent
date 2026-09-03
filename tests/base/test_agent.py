from __future__ import annotations

import asyncio
import tempfile
from pathlib import Path

import pytest

from prodagent import Agent, AgentConfig, HardBudget, RunState
from prodagent.kernel.bus import HookRegistry
from prodagent.llm.fake import script
from prodagent.tooling import tool


def _simple_agent(llm, **kwargs) -> Agent:
    return Agent(
        "test-agent",
        system_prompt="Verify system status.",
        config=AgentConfig(name="test-agent", llm=llm, hooks=HookRegistry()),
        **kwargs,
    )


def test_agent_creation():
    llm = script({"content": "all good"})
    agent = _simple_agent(llm)
    assert agent.name == "test-agent"
    assert agent.config.initial_plan is None  # no preset graph: chat runs the agent itself


def test_agent_run_completes():
    llm = script({"content": "Task completed successfully."})
    agent = _simple_agent(llm)
    run = asyncio.run(agent.chat("check system health"))
    assert run.state == RunState.COMPLETED
    assert run.final_output is not None


def test_agent_run_returns_agent_run():
    from prodagent.kernel.run import Run

    llm = script({"content": "done"})
    agent = _simple_agent(llm)
    run = asyncio.run(agent.chat("any task"))
    assert isinstance(run, Run)


def test_agent_with_tools():
    @tool(name="health_check", readonly=True)
    async def health_check(service: str) -> dict:
        return {"healthy": True, "service": service}

    llm = script(
        {"tool": "health_check", "params": {"service": "api"}},
        {"content": "Service is healthy."},
    )
    agent = Agent(
        "ops",
        system_prompt="Run health check.",
        tools=[health_check],
        config=AgentConfig(name="ops", llm=llm, hooks=HookRegistry()),
    )
    run = asyncio.run(agent.chat("Is the API healthy?"))
    assert run.state == RunState.COMPLETED
    assert any(tc.name == "health_check" for tc in run.tool_history)


def test_agent_constraints_in_system_prompt():
    captured_systems: list[str] = []

    from prodagent.kernel.types import LLMResponse
    from prodagent.llm import LLMClient

    class CaptureLLM(LLMClient):
        async def complete(self, messages, *, system="", tools=None, config=None, on_chunk):
            captured_systems.append(system)
            return LLMResponse(content="done", stop_reason="end_turn")

    agent = Agent(
        "constrained",
        system_prompt="Check things.",
        config=AgentConfig(
            name="constrained",
            constraints=["ALWAYS validate input", "NEVER skip logging"],
            llm=CaptureLLM(),
            hooks=HookRegistry(),
        ),
    )
    asyncio.run(agent.chat("check"))

    assert any("ALWAYS validate input" in s for s in captured_systems)
    assert any("NEVER skip logging" in s for s in captured_systems)


def test_agent_context_in_system_prompt():
    captured_systems: list[str] = []

    from prodagent.kernel.types import LLMResponse
    from prodagent.llm import LLMClient

    class CaptureLLM(LLMClient):
        async def complete(self, messages, *, system="", tools=None, config=None, on_chunk):
            captured_systems.append(system)
            return LLMResponse(content="done", stop_reason="end_turn")

    agent = Agent(
        "ctx-agent",
        system_prompt="Incident INC-001: payment service down",
        config=AgentConfig(name="ctx-agent", llm=CaptureLLM(), hooks=HookRegistry()),
    )
    asyncio.run(agent.chat("handle incident"))

    assert any("INC-001" in s for s in captured_systems)


def test_fluent_api_inject():
    from prodagent.kernel.bus import InjectionPoint

    agent = Agent(
        "test-agent",
        config=AgentConfig(
            name="test-agent",
            llm=script({"content": "ok"}),
            injectors=[(InjectionPoint.CONTEXT_INJECTOR, lambda q: f"Context: {q}")],
        ),
    )
    assert len(agent.config.injectors) == 1


def test_extend_human_approval_registers_checker():
    from prodagent.hooks.approval import ApprovalGate
    from prodagent.hooks.bundles.security import ApprovalHooks
    from prodagent.kernel.bus import Gate, HookRegistry

    hooks = HookRegistry()
    ApprovalHooks(gate=ApprovalGate()).attach(hooks)
    assert len(hooks.check_handlers(Gate.APPROVAL_REQUEST)) == 1


def test_fluent_api_budget():
    agent = Agent(
        "test-agent",
        budget=HardBudget(max_turns=10, max_cost_usd=0.5, max_seconds=120.0),
        config=AgentConfig(name="test-agent", llm=script({"content": "ok"})),
    )
    assert agent.budget_config is not None
    assert agent.budget_config.max_turns == 10


def test_fluent_api_drafting_vs_bare():
    from prodagent.plan.planner import Planner

    agent = Agent(
        "test-agent",
        config=AgentConfig(
            name="test-agent",
            llm=script({"content": "ok"}),
            planner=Planner(script({"content": "ok"})),
        ),
    )
    assert agent.config.planner is not None  # drafts a graph per turn
    agent2 = Agent(
        "test-agent-2",
        config=AgentConfig(name="test-agent-2", llm=script({"content": "ok"})),
    )
    assert agent2.config.planner is None  # bare agent: chat runs the agent itself


def test_agent_saves_to_session_dir():
    from dataclasses import replace as _dc_replace

    from prodagent.base.config import FrameworkConfig

    llm = script({"content": "saved run complete"})

    with tempfile.TemporaryDirectory() as tmpdir:
        from prodagent.base.config import production

        fw = _dc_replace(
            production(FrameworkConfig.default()),
            orchestration=_dc_replace(
                FrameworkConfig.default().orchestration,
                runs_dir=str(Path(tmpdir) / "checkpoints"),
                sessions_dir=str(Path(tmpdir) / "sessions"),
            ),
        )
        agent = Agent(
            "session-agent",
            system_prompt="Do some work.",
            config=AgentConfig(name="session-agent", llm=llm, hooks=HookRegistry(), framework=fw),
        )
        run = asyncio.run(agent.chat("save this run", session_id="run-save-001"))
        assert run.state == RunState.COMPLETED
        assert run.run_id == "run-save-001:1"

        assert (Path(tmpdir) / "checkpoints" / "run-save-001:1.v1.json").exists()


def test_agent_budget_respected():
    from prodagent.kernel.types import LLMResponse
    from prodagent.llm import LLMClient

    class InfiniteLoopLLM(LLMClient):
        async def complete(self, messages, *, system="", tools=None, config=None, on_chunk):
            return LLMResponse(content="thinking...", stop_reason="end_turn")

    agent = Agent(
        "budget-test",
        system_prompt="Keep going.",
        budget=HardBudget(max_turns=2, max_seconds=30.0),
        config=AgentConfig(name="budget-test", llm=InfiniteLoopLLM(), hooks=HookRegistry()),
    )
    run = asyncio.run(agent.chat("test budget"))
    assert run.state in (RunState.COMPLETED, RunState.FAILED)
    assert run.turn_count <= 3


def test_agent_stream_yields_events():
    from prodagent.kernel.types import RunCompletedEvent
    from prodagent.tooling import tool as tool_decorator

    @tool_decorator(name="probe", readonly=True)
    async def probe(target: str) -> dict:
        return {"alive": True}

    llm = script(
        {"tool": "probe", "params": {"target": "db"}},
        {"content": "All systems operational."},
    )
    agent = Agent(
        "stream-test",
        system_prompt="Check systems.",
        tools=[probe],
        config=AgentConfig(name="stream-test", llm=llm, hooks=HookRegistry()),
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
    from prodagent.kernel.types import LLMResponse, RunFailedEvent
    from prodagent.llm import LLMClient

    class LoopingLLM(LLMClient):
        async def complete(self, messages, *, system="", tools=None, config=None, on_chunk):
            return LLMResponse(content="still thinking...", stop_reason="end_turn")

    agent = Agent(
        "budget-stream",
        system_prompt="Loop forever.",
        budget=HardBudget(max_turns=2, max_seconds=30.0),
        config=AgentConfig(name="budget-stream", llm=LoopingLLM(), hooks=HookRegistry()),
    )

    events = []

    async def drain():
        async for ev in agent.chat_stream("run until budget"):
            events.append(ev)

    asyncio.run(drain())

    final = events[-1] if events else None
    assert final is not None
    from prodagent.kernel.types import RunCompletedEvent

    assert isinstance(final, RunCompletedEvent | RunFailedEvent)


@pytest.mark.asyncio
async def test_plan_first_failed_run_does_not_raise_attribute_error():
    from prodagent.kernel.types import LLMResponse, RunFailedEvent
    from prodagent.llm.fake import FakeLLMAdapter

    bad_plan_llm = FakeLLMAdapter(
        responses=[LLMResponse(content="not valid json", stop_reason="end_turn")]
    )

    from prodagent.plan.planner import Planner

    agent = Agent(
        "plan-fail",
        system_prompt="Parse my plan.",
        config=AgentConfig(
            name="plan-fail", llm=bad_plan_llm, hooks=HookRegistry(), planner=Planner(bad_plan_llm)
        ),
    )
    assert agent.config.planner is not None

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
        Agent(
            "bad::name",
            system_prompt="",
            config=AgentConfig(name="bad::name", llm=script({"content": "x"})),
        )
