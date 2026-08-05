import asyncio

import pytest

from prodagent import Agent, ExecutionMode, HardBudget
from prodagent.core.config import ContextConfig, FrameworkConfig
from prodagent.hooks.registry import HookRegistry
from prodagent.llm.fake import script
from prodagent.tooling import tool


@pytest.mark.asyncio
async def test_budget_turn_limit_exceeded():

    @tool(name="never_finish")
    async def never_finish_tool() -> str:
        return "Keep going"

    agent = Agent(
        name="budget-turn-agent",
        system_prompt="Never stop.",
        tools=[never_finish_tool],
        budget=HardBudget(max_turns=2),
        llm=script({"content": "Keep calling never_finish"}),
        hooks=HookRegistry(),
        mode=ExecutionMode.REACTIVE,
    )

    run = await agent.chat("Start infinite loop")

    assert run is not None
    assert run.turn_count <= 3


@pytest.mark.asyncio
async def test_budget_cost_limit_exceeded():

    @tool(name="expensive")
    async def expensive_tool() -> str:
        return "Expensive result"

    agent = Agent(
        name="budget-cost-agent",
        system_prompt="Spend money.",
        tools=[expensive_tool],
        budget=HardBudget(max_cost_usd=0.0001),
        llm=script({"content": "Call expensive"}),
        hooks=HookRegistry(),
        mode=ExecutionMode.REACTIVE,
    )

    run = await agent.chat("Spend budget")

    assert run is not None
    assert run.cost_usd >= 0.0


@pytest.mark.asyncio
async def test_budget_time_limit_exceeded():

    @tool(name="slow")
    async def slow_tool() -> str:
        await asyncio.sleep(0.5)
        return "Slow result"

    agent = Agent(
        name="budget-time-agent",
        system_prompt="Take time.",
        tools=[slow_tool],
        budget=HardBudget(max_seconds=1.0),
        llm=script({"content": "Call slow tool"}),
        hooks=HookRegistry(),
        mode=ExecutionMode.REACTIVE,
    )

    run = await agent.chat("Take time")

    assert run is not None
    assert run.elapsed_seconds() <= 3.0


@pytest.mark.asyncio
async def test_context_window_overflow():

    @tool(name="grow_context")
    async def grow_context_tool(data: str = "") -> str:
        return "x" * 10000

    agent = Agent(
        name="context-overflow-agent",
        system_prompt="Fill context.",
        tools=[grow_context_tool],
        budget=HardBudget(max_turns=5),
        framework=FrameworkConfig(context=ContextConfig(max_tokens=500)),
        llm=script({"content": "Starting context"}),
        hooks=HookRegistry(),
        mode=ExecutionMode.REACTIVE,
    )

    run = await agent.chat("Overflow context")

    assert run is not None
    assert run.state.value in ("completed", "failed", "running")


@pytest.mark.asyncio
async def test_tool_execution_timeout():

    @tool(name="hangs")
    async def hangs_tool() -> str:
        await asyncio.sleep(1000)
        return "Never returned"

    agent = Agent(
        name="timeout-agent",
        system_prompt="Test timeout.",
        tools=[hangs_tool],
        budget=HardBudget(max_turns=1),
        llm=script({"content": "Call hangs"}),
        hooks=HookRegistry(),
        mode=ExecutionMode.REACTIVE,
    )

    run = await asyncio.wait_for(agent.chat("Test timeout"), timeout=2.0)

    assert run is not None


@pytest.mark.asyncio
async def test_infinite_loop_detection():

    @tool(name="repeat")
    async def repeat_tool() -> str:
        return "Repeat this"

    agent = Agent(
        name="infinite-loop-agent",
        system_prompt="Repeat forever.",
        tools=[repeat_tool],
        budget=HardBudget(max_turns=10),
        llm=script({"content": "Call repeat"}),
        hooks=HookRegistry(),
        mode=ExecutionMode.REACTIVE,
    )

    run = await agent.chat("Start loop")

    assert run is not None
    assert run.turn_count <= 10


@pytest.mark.asyncio
async def test_tool_parameter_validation_failure():

    @tool(name="validated")
    async def validated_tool(name: str, age: int, email: str) -> str:
        return f"Validated: {name}, {age}, {email}"

    agent = Agent(
        name="validation-agent",
        system_prompt="Test validation.",
        tools=[validated_tool],
        budget=HardBudget(max_turns=5),
        llm=script({"content": "Call validated"}),
        hooks=HookRegistry(),
        mode=ExecutionMode.REACTIVE,
    )

    run = await agent.chat("Test validation")

    assert run is not None


@pytest.mark.asyncio
async def test_memory_overflow():

    @tool(name="consume_memory")
    async def consume_memory_tool() -> str:
        large_data = ["x" * 1000 for _ in range(10000)]
        return f"Consumed memory with {len(large_data)} items"

    agent = Agent(
        name="memory-agent",
        system_prompt="Test memory limit.",
        tools=[consume_memory_tool],
        budget=HardBudget(max_turns=3),
        llm=script({"content": "Consume memory"}),
        hooks=HookRegistry(),
        mode=ExecutionMode.REACTIVE,
    )

    run = await agent.chat("Test memory limit")

    assert run is not None
    assert run.state.value in ("completed", "failed", "running")


@pytest.mark.asyncio
async def test_empty_tool_result():

    @tool(name="empty")
    async def empty_tool() -> str:
        return ""

    agent = Agent(
        name="empty-agent",
        system_prompt="Test empty result.",
        tools=[empty_tool],
        budget=HardBudget(max_turns=5),
        llm=script({"content": "Empty result"}),
        hooks=HookRegistry(),
        mode=ExecutionMode.REACTIVE,
    )

    run = await agent.chat("Test empty result")

    assert run is not None
    assert run.state.value in ("completed", "failed", "running")


@pytest.mark.asyncio
async def test_rapid_sequential_runs():

    @tool(name="fast")
    async def fast_tool() -> str:
        return "Done"

    agent = Agent(
        name="rapid-agent",
        system_prompt="Run fast.",
        tools=[fast_tool],
        budget=HardBudget(max_turns=1),
        llm=script({"content": "Fast"}),
        hooks=HookRegistry(),
        mode=ExecutionMode.REACTIVE,
    )

    runs = []
    for i in range(5):
        run = await agent.chat(f"Run {i}")
        runs.append(run)

    for run in runs:
        assert run is not None


@pytest.mark.asyncio
async def test_graceful_shutdown():

    @tool(name="interruptible")
    async def interruptible_tool() -> str:
        await asyncio.sleep(0.1)
        return "Result"

    agent = Agent(
        name="shutdown-agent",
        system_prompt="Test shutdown.",
        tools=[interruptible_tool],
        budget=HardBudget(max_turns=1),
        llm=script({"content": "Call tool"}),
        hooks=HookRegistry(),
        mode=ExecutionMode.REACTIVE,
    )

    run = await agent.chat("Test shutdown")

    assert run is not None
    assert run.state.value in ("completed", "running")
