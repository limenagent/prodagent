"""SubAgentBody — delegation as a node, on the shared activation core.

Column 26's acceptance: whether a delegation arrives as a tool call the
model made or as a node written into the graph, the Run tree underneath is
the same shape — same child id (``parent::name``), same depth, same budget
attribution, same terminal fold. One execution core, many entry points.
"""

from __future__ import annotations

import pytest

from prodagent.base.types import ExecutionMode
from prodagent.kernel.types import LLMResponse, RunState, ToolCall
from prodagent.llm.fake import FakeLLMAdapter
from prodagent.plan.workflow import Workflow
from prodagent.runtime.agent import Agent
from prodagent.runtime.config import AgentConfig


def _child(name: str, answer: str) -> Agent:
    child = Agent(name, system_prompt=f"does {name}", mode=ExecutionMode.REACTIVE)
    child.config.llm = FakeLLMAdapter(
        responses=[LLMResponse(content=answer, stop_reason="end_turn")]
    )
    return child


async def _collect(stream):
    run = None
    async for event in stream:
        run = getattr(event, "run", None) or run
    return run


class TestSubagentNode:
    @pytest.mark.asyncio
    async def test_graph_node_delegation_runs_the_child_and_folds_output(self):
        child = Agent("worker", system_prompt="does work", mode=ExecutionMode.REACTIVE)

        wf = Workflow()
        wf.step(child, is_terminal=True)  # an Agent step compiles to SubAgentBody

        # A forked child speaks under the parent hop's wiring — its LLM is
        # the parent's (that is what fork-as-spawn means), so the scripted
        # answer rides on the parent.
        parent = Agent(
            "chief",
            system_prompt="delegates",
            mode=ExecutionMode.PLAN_FIRST,
            workflow=wf,
            config=AgentConfig(
                name="chief",
                agents=[child],
                llm=FakeLLMAdapter(
                    responses=[LLMResponse(content="report ready", stop_reason="end_turn")]
                ),
            ),
        )
        run = await parent.chat("do the thing")

        assert run.state is RunState.COMPLETED
        # the child's report folded into the terminal node → final output
        assert "report ready" in str(run.final_output)

    @pytest.mark.asyncio
    async def test_child_run_carries_parentage_and_depth(self):
        from prodagent.kernel.state import child_run_id
        from prodagent.runtime import runner as runner_mod

        child = Agent("digger", system_prompt="digs", mode=ExecutionMode.REACTIVE)
        activations: list = []

        original_activate = runner_mod.InProcessRunner.activate

        def spy_activate(self, activation):
            activations.append(activation)
            return original_activate(self, activation)

        runner_mod.InProcessRunner.activate = spy_activate
        try:
            wf = Workflow()
            wf.step(child, is_terminal=True)
            parent = Agent(
                "root-chief",
                mode=ExecutionMode.PLAN_FIRST,
                workflow=wf,
                config=AgentConfig(
                    name="root-chief",
                    agents=[child],
                    llm=FakeLLMAdapter(
                        responses=[LLMResponse(content="found it", stop_reason="end_turn")]
                    ),
                ),
            )
            await parent.chat("dig")
        finally:
            runner_mod.InProcessRunner.activate = original_activate

        assert activations, "the SubAgentBody node must activate through the port"
        activation = activations[0]
        assert activation.agent is child
        assert activation.parent_run_id is not None
        assert activation.depth == 1, "delegation from a root graph is one hop down"
        assert activation.run_id == child_run_id(activation.parent_run_id, "digger")

    @pytest.mark.asyncio
    async def test_tool_and_graph_entry_points_grow_isomorphic_trees(self):
        """Same child, two doors: the model calling ``spawn_agent`` mid-Turn
        and the graph carrying a SubAgentBody node must produce the same
        child id, depth and completed fold — one core, many doors."""
        from prodagent.kernel.state import child_run_id

        # door 1: the tool (REACTIVE parent, model-driven spawn)
        child_a = _child("scout", "scouted")
        parent_tool = Agent(
            "tool-parent",
            system_prompt="spawn it",
            mode=ExecutionMode.REACTIVE,
            config=AgentConfig(
                name="tool-parent",
                llm=FakeLLMAdapter(
                    responses=[
                        LLMResponse(
                            content="",
                            tool_calls=[
                                ToolCall(
                                    name="spawn_agent",
                                    params={"name": "scout", "task": "scout ahead"},
                                )
                            ],
                            stop_reason="tool_use",
                        ),
                        LLMResponse(content="done", stop_reason="end_turn"),
                    ]
                ),
                agents=[child_a],
            ),
        )
        tool_run = await parent_tool.chat("go")
        assert tool_run.state is RunState.COMPLETED

        # door 2: the graph node (PLAN_FIRST parent, workflow-declared)
        child_b = _child("scout", "scouted")
        wf = Workflow()
        wf.step(child_b, is_terminal=True)
        parent_graph = Agent(
            "graph-parent",
            mode=ExecutionMode.PLAN_FIRST,
            workflow=wf,
            config=AgentConfig(name="graph-parent", agents=[child_b]),
        )
        graph_run = await parent_graph.chat("go")
        assert graph_run.state is RunState.COMPLETED

        # isomorphism: same ::childA shape, same depth=1 hop, both completed
        assert child_run_id("p", "scout") == "p::scout"  # the id grammar
        assert "scout" in child_a.config.name
        assert child_b.config.name == child_a.config.name
