import asyncio

import pytest

from prodagent.core.types import ExecutionMode
from prodagent.llm.fake import script
from prodagent.runtime.agent import Agent
from prodagent.tooling.base import FunctionTool


async def test_spawn_tool_respects_duplicate_message_ids():

    async def dummy_tool(x: int) -> int:
        return x * 2

    tool = FunctionTool(name="double", fn=dummy_tool, meta=None, schema={})
    child = (
        Agent("doubler", system_prompt="Double the number", tools=[tool])
        .description("Doubles a number")
        .reactive()
    )

    fake_llm = script(
        {"tool": "double", "params": {"x": 5}},
        {"content": "Result: 10"},
    )

    from prodagent.runtime.coordination.spawn import build_spawn_tools_for_agent

    spawn_tools = build_spawn_tools_for_agent([child], llm=fake_llm)
    spawn_tool = spawn_tools.tool

    result1 = await spawn_tool._fn(name="doubler", task="double 5", idempotency_key="retry-key-1")
    assert result1["state"] != "duplicate"

    result2 = await spawn_tool._fn(name="doubler", task="double 5", idempotency_key="retry-key-1")
    assert result2["state"] == "duplicate"
    assert "duplicate" in result2["output"].lower()
    assert spawn_tools.accumulator.spawn_count == 1, "the retry must not re-spawn the child"


async def test_spawn_tool_different_tasks_not_duplicated():

    async def dummy_tool(x: int) -> int:
        return x * 2

    tool = FunctionTool(name="double", fn=dummy_tool, meta=None, schema={})
    child = (
        Agent("doubler", system_prompt="Double the number", tools=[tool])
        .description("Doubles a number")
        .reactive()
    )

    fake_llm = script({"tool": "double", "params": {"x": 5}}, {"content": "Result: 10"})

    from prodagent.runtime.coordination.spawn import build_spawn_tools_for_agent

    spawn_tools = build_spawn_tools_for_agent([child], llm=fake_llm)
    spawn_tool = spawn_tools.tool

    result1 = await spawn_tool._fn(name="doubler", task="task A")
    result2 = await spawn_tool._fn(name="doubler", task="task B")

    assert result1["state"] != "duplicate"
    assert result2["state"] != "duplicate"


async def test_spawn_tool_creates_handoff_packet():
    child = Agent("echoer", system_prompt="Echo").description("Echoes input").reactive()

    fake_llm = script({"content": "Echo: test"})

    from prodagent.runtime.coordination.spawn import build_spawn_tools_for_agent

    spawn_tools = build_spawn_tools_for_agent([child], llm=fake_llm)
    spawn_tool = spawn_tools.tool

    result = await spawn_tool._fn(name="echoer", task="echo test")

    assert result["agent"] == "echoer"


async def test_spawn_tool_result_has_output_and_state():
    child = (
        Agent("bad_agent", system_prompt="Returns malformed result")
        .description("Returns malformed result")
        .reactive()
    )

    fake_llm = script({"content": ""})

    from prodagent.runtime.coordination.spawn import build_spawn_tools_for_agent

    spawn_tools = build_spawn_tools_for_agent([child], llm=fake_llm)
    spawn_tool = spawn_tools.tool

    result = await spawn_tool._fn(name="bad_agent", task="fail")

    assert "output" in result
    assert "state" in result


def run_async_test(test_func):
    return asyncio.run(test_func())


def _spec(name="worker"):
    return Agent(name, system_prompt="work").description("worker").reactive()


async def test_spawn_tool_meta_allows_parallel_and_idempotent():
    from prodagent.runtime.coordination.spawn import build_spawn_tools_for_agent

    fake_llm = script({"content": "done"})
    spawn = build_spawn_tools_for_agent([_spec()], llm=fake_llm)
    assert spawn is not None
    assert spawn.tool.meta.is_readonly is True
    assert spawn.tool.meta.side_effect_level.value == "low"
    assert spawn.tool.meta.enforced_idempotent is True
    assert spawn.accumulator is not None


async def test_spawn_aggregates_cost_into_accumulator():
    from prodagent.runtime.coordination.fork import ParentRuntime
    from prodagent.runtime.coordination.spawn import build_spawn_tools_for_agent

    fake_llm = script({"content": "done"})
    ctx = ParentRuntime()
    spawn = build_spawn_tools_for_agent([_spec()], llm=fake_llm, context=ctx)
    result = await spawn.tool._fn(name="worker", task="do it")
    assert result["state"] != "duplicate"
    assert ctx.accumulator.spawn_count == 1
    assert ctx.accumulator.turns >= 0
    assert ctx.accumulator is spawn.accumulator


async def test_l7_handoff_rejection_dead_letters(monkeypatch=None):
    from prodagent.core.exceptions import SecurityViolation
    from prodagent.hooks.checkpoint import CheckPoint
    from prodagent.hooks.registry import HookRegistry
    from prodagent.runtime.coordination.spawn import build_spawn_tools_for_agent

    hooks = HookRegistry()

    def _reject(*, handoff_data=None, **_):
        raise SecurityViolation("policy: forbidden next_action")

    hooks.register_checker(CheckPoint.AGENT_HANDOFF, _reject)

    fake_llm = script({"content": "done"})
    spawn = build_spawn_tools_for_agent([_spec()], llm=fake_llm, hooks=hooks)
    result = await spawn.tool._fn(name="worker", task="do it")
    assert result["state"] == "handoff_rejected"
    assert "security policy" in result["output"].lower()


async def test_security_veto_from_child_propagates_not_swallowed():
    from prodagent.core.exceptions import PermissionDenied
    from prodagent.runtime.coordination.spawn import build_spawn_tools_for_agent

    class _VetoLLM:
        async def complete(self, *a, **k):
            raise PermissionDenied("child-side veto")

        async def stream(self, *a, **k):  # pragma: no cover - not reached
            raise PermissionDenied("child-side veto")

        def estimate_cost(self, *a, **k):
            return 0.0

    spawn = build_spawn_tools_for_agent([_spec()], llm=_VetoLLM())
    with pytest.raises(PermissionDenied):
        await spawn.tool._fn(name="worker", task="do it")


async def test_child_agent_preserves_reactive_mode():
    child = Agent("child", system_prompt="child task").reactive()

    rebuilt = Agent(
        child.name,
        tools=list(child.inline_tools),
        system_prompt=child.system_prompt,
        mode=child.mode,
    )
    assert rebuilt.mode == ExecutionMode.REACTIVE
    assert rebuilt.child_agents == []
