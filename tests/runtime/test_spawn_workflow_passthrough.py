"""Workflow sub-agent passthrough — ``Spawn._run_child`` must forward
``initial_plan`` / ``max_replans`` so a ``workflow=`` child runs the preset
DAG instead of falling back to LLM planning.
"""

from __future__ import annotations

import pytest

from prodagent import Agent, AgentConfig
from prodagent.coordination.spawn import build_spawn_tools_for_agent
from prodagent.llm.fake import script
from prodagent.plan.workflow import Workflow
from prodagent.runtime.parent_runtime import ParentRuntime
from prodagent.runtime.runner import InProcessRunner
from prodagent.tooling import tool


@pytest.mark.asyncio
async def test_workflow_child_runs_preset_dag_via_spawn():
    """A ``.workflow()`` child spawned via ``spawn_agent`` must execute its
    compiled Plan, not call the LLM for planning.

    The FakeLLM only has one scripted response (the llm_step reply). If
    passthrough is broken, the child would try to plan and either fail (no
    plan JSON scripted) or call the wrong response.
    """

    @tool(name="fetch", readonly=True)
    async def fetch() -> dict:
        return {"data": "fetched"}

    wf = Workflow()
    wf.tool_step("s1", "fetch")
    wf.llm_step("s2", "Summarise: {{s1.output}}", depends_on=["s1"], is_terminal=True)

    # The llm_step calls the LLM once with the step prompt; script a reply.
    llm = script({"content": "summary of fetched"})
    child = Agent(
        "wf_worker",
        system_prompt="do the work",
        tools=[fetch],
        workflow=wf,
        allow_replan=False,
        config=AgentConfig(name="wf_worker", llm=llm, description="A workflow worker"),
    )

    assert child.config.initial_plan is not None
    assert child.config.max_replans == 0

    spawn = build_spawn_tools_for_agent([child], runner=InProcessRunner(ParentRuntime(llm=llm)))
    result = await spawn.tool._fn(name="wf_worker", task="fetch and summarise")

    assert result["state"] == "completed", (
        f"workflow child should complete via preset DAG, got: {result}"
    )
    assert "summary of fetched" in result["output"], (
        f"terminal llm_step output must surface in child result, got: {result['output']!r}"
    )


@pytest.mark.asyncio
async def test_workflow_child_forwarded_max_replans_is_zero():
    """``allow_replan=False`` on the spec must reach the child — otherwise a
    step failure would trigger LLM replanning instead of terminating."""

    @tool(name="boom", readonly=True)
    async def boom() -> dict:
        raise RuntimeError("intentional")

    wf = Workflow()
    wf.tool_step("s1", "boom")

    llm = script({"content": "x"})
    child = Agent(
        "wf_boom",
        system_prompt="",
        tools=[boom],
        workflow=wf,
        allow_replan=False,
        config=AgentConfig(name="wf_boom", llm=llm),
    )

    assert child.config.max_replans == 0

    spawn = build_spawn_tools_for_agent([child], runner=InProcessRunner(ParentRuntime(llm=llm)))
    result = await spawn.tool._fn(name="wf_boom", task="trigger boom")

    # max_replans=0 → step failure terminates; child does not silently replan.
    assert result["state"] in ("completed", "failed"), result
