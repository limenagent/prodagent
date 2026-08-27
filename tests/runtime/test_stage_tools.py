"""Stage tools end-to-end — the model convenes an ensemble and runs a work
queue through the same tools it spawns children with. Specs are declared on
AgentConfig; the hop assembler mounts the tools; FakeLLM scripts the calls."""

from __future__ import annotations

import pytest

from prodagent import Agent, AgentConfig, ExecutionMode
from prodagent.coordination.ensemble import AgentFloorMember, EnsembleSpec
from prodagent.coordination.infra.stage import MaxRounds, TerminationPolicy
from prodagent.coordination.infra.stage_tools import build_stage_tools
from prodagent.coordination.work_queue import AgentWorkMember, WorkQueueSpec
from prodagent.kernel.types import LLMResponse, StopReason, ToolCall
from prodagent.llm.fake import FakeLLMAdapter
from prodagent.runtime.config import AgentConfig as RuntimeAgentConfig  # noqa: F401 — same class


def _speaker(name: str, text: str) -> Agent:
    return Agent(
        name,
        mode=ExecutionMode.REACTIVE,
        config=AgentConfig(
            name=name,
            llm=FakeLLMAdapter(responses=[LLMResponse(content=text, stop_reason="end_turn")]),
        ),
    )


@pytest.mark.asyncio
async def test_model_convenes_a_declared_ensemble_through_the_tool():
    panel = EnsembleSpec(
        name="panel",
        members=[
            AgentFloorMember(_speaker("pro", "support it"), session_id="panel-pro"),
            AgentFloorMember(_speaker("con", "oppose it"), session_id="panel-con"),
        ],
        topic="should we ship on Friday?",
        termination=TerminationPolicy(hard_cap=MaxRounds(max_rounds=2)),
    )
    moderator = Agent(
        "moderator",
        system_prompt="convene the panel",
        mode=ExecutionMode.REACTIVE,
        config=AgentConfig(
            name="moderator",
            llm=FakeLLMAdapter(
                responses=[
                    LLMResponse(
                        content="",
                        tool_calls=[
                            ToolCall(
                                name="run_ensemble",
                                params={"name": "panel", "task": "should we ship on Friday?"},
                            )
                        ],
                        stop_reason=StopReason.TOOL_USE,
                    ),
                    LLMResponse(content="panel done", stop_reason="end_turn"),
                ]
            ),
            ensembles=[panel],
        ),
    )

    run = await moderator.chat("decide the Friday release", session_id="stage-tool-1")

    assert run.state.value == "completed", run.last_error
    # The tool's summary rode back into the transcript as the tool result.
    tool_results = [
        m for m in run.messages if m.get("role") == "tool" and "panel" in str(m.get("content", ""))
    ]
    assert tool_results, "run_ensemble result must land in the transcript"
    summary = tool_results[0]["content"]
    assert '"pro"' in summary and "support it" in summary
    assert '"con"' in summary and "oppose it" in summary


@pytest.mark.asyncio
async def test_model_runs_a_declared_work_queue_through_the_tool():
    worker = _speaker("w1", "handled")
    queue = WorkQueueSpec(
        name="chores",
        workers={"w1": AgentWorkMember(worker)},
        items=[],
    )
    dispatcher_agent = Agent(
        "boss",
        system_prompt="fan out the chores",
        mode=ExecutionMode.REACTIVE,
        config=AgentConfig(
            name="boss",
            llm=FakeLLMAdapter(
                responses=[
                    LLMResponse(
                        content="",
                        tool_calls=[
                            ToolCall(
                                name="run_work_queue",
                                params={"name": "chores", "items": ["wash", "dry"]},
                            )
                        ],
                        stop_reason=StopReason.TOOL_USE,
                    ),
                    LLMResponse(content="chores done", stop_reason="end_turn"),
                ]
            ),
            work_queues=[queue],
        ),
    )

    run = await dispatcher_agent.chat("do the chores", session_id="stage-tool-2")

    assert run.state.value == "completed", run.last_error
    tool_results = [
        m for m in run.messages if m.get("role") == "tool" and "chores" in str(m.get("content", ""))
    ]
    assert tool_results, "run_work_queue result must land in the transcript"
    summary = tool_results[0]["content"]
    assert '"0"' in summary and '"1"' in summary, "both items must complete"
    assert '"dead_lettered": 0' in summary


@pytest.mark.asyncio
async def test_model_runs_a_declared_blackboard_through_the_tool():
    from prodagent.coordination.blackboard import BlackboardSpec, Trigger

    class _SeededExpert:
        def __init__(self, name: str, key: str) -> None:
            self.name = name
            self._key = key

        async def try_contribute(self, board, *, trigger):
            from prodagent.coordination.blackboard import BoardWrite

            if board.version_of(self._key) > 0:
                return None
            return BoardWrite(key=self._key, value=f"by-{self.name}", author=self.name)

    board_spec = BlackboardSpec(
        name="facts",
        experts={"e1": _SeededExpert("e1", "answer")},
        triggers={"kickoff": Trigger(name="kickoff", keys=[], experts=["e1"], mode="event")},
    )
    host = Agent(
        "host",
        system_prompt="seed the board",
        mode=ExecutionMode.REACTIVE,
        config=AgentConfig(
            name="host",
            llm=FakeLLMAdapter(
                responses=[
                    LLMResponse(
                        content="",
                        tool_calls=[
                            ToolCall(
                                name="run_blackboard",
                                params={"name": "facts", "seeds": {"question": "2+2?"}},
                            )
                        ],
                        stop_reason=StopReason.TOOL_USE,
                    ),
                    LLMResponse(content="board done", stop_reason="end_turn"),
                ]
            ),
            blackboards=[board_spec],
        ),
    )

    run = await host.chat("get the answer", session_id="stage-tool-3")

    assert run.state.value == "completed", run.last_error
    tool_results = [
        m for m in run.messages if m.get("role") == "tool" and "facts" in str(m.get("content", ""))
    ]
    assert tool_results, "run_blackboard result must land in the transcript"
    summary = tool_results[0]["content"]
    assert "question" in summary, "the seed slot must be on the board"
    assert "by-e1" in summary, "the expert's write must be on the board"


@pytest.mark.asyncio
async def test_unnamed_specs_are_not_callable_and_unknown_names_error():
    from prodagent.coordination.ensemble import RoundRobin

    unnamed = EnsembleSpec(
        members=[AgentFloorMember(_speaker("a", "x"), session_id="s")],
        topic="t",
        order=RoundRobin(),
    )
    tools = build_stage_tools(ensembles=[unnamed])
    assert tools == [], "a spec without a name has no tool handle"

    named = EnsembleSpec(
        name="panel",
        members=[AgentFloorMember(_speaker("a", "x"), session_id="s")],
        topic="t",
    )
    (tool,) = build_stage_tools(ensembles=[named])

    result = await tool._fn(name="nope")
    assert result["error"] is True and result["known"] == ["panel"]
