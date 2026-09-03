"""react recipe — ReAct as two nodes and a back edge (column 23).

The law under test: a "ReAct agent" reduced to the graph vocabulary is two
nodes (think + tools), one conditional edge (think → tools only when the
model asked), and one back edge (tools → think). The model's only power is
voting on the conditional edge; the loop's skeleton is edges. Driven by the
one Scheduler, it turns until the model stops asking for tools.
"""

from __future__ import annotations

from prodagent.kernel.scheduler import Scheduler
from prodagent.kernel.types import LLMResponse, ToolCall, ToolOutcome, ToolResult
from prodagent.runtime.recipes.react import (
    DISPATCHER_KEY,
    LLM_CLIENT_KEY,
    build_react_plan,
)


class _ScriptLLM:
    """First call asks for a tool, second call (after the result) finishes."""

    def __init__(self) -> None:
        self.calls = 0
        self.default_config = None

    async def complete(self, messages, *, system="", tools=None, config=None, **kw):
        self.calls += 1
        if self.calls == 1:
            return LLMResponse(
                content="",
                tool_calls=[ToolCall(name="lookup", params={"q": "x"}, call_id="c1")],
                stop_reason="tool_use",
                input_tokens=10,
                output_tokens=5,
            )
        return LLMResponse(
            content="the answer is 42",
            stop_reason="end_turn",
            input_tokens=10,
            output_tokens=8,
        )


class _StubDispatcher:
    async def dispatch(self, call, *, run_id=""):
        return ToolResult(ToolOutcome.OK, value=f"looked up {call.name}", tool=call.name)


async def test_react_is_two_nodes_plus_a_back_edge():
    plan = build_react_plan(system="be helpful")
    assert set(plan.nodes) == {"think", "tools"}
    back = plan.back_edges()
    assert {(e.source, e.target) for e in back} == {("tools", "think")}
    # the exit is the conditional edge: no pending calls ⇒ the tools edge waives
    cond = next(e for e in plan.edges if e.source == "think" and e.target == "tools")
    assert cond.when is not None


async def test_react_loop_turns_until_the_model_stops_asking():
    llm = _ScriptLLM()
    plan = build_react_plan(system="be helpful")
    sched = Scheduler(
        initial_plan=plan,
        wiring={LLM_CLIENT_KEY: llm, DISPATCHER_KEY: _StubDispatcher()},
    )
    terminal = None
    async for ev in sched.stream("what is the answer?"):
        terminal = ev
    assert terminal.run.state.value == "completed"
    assert llm.calls == 2  # one tool round, then the final answer
    assert terminal.run.final_output == "the answer is 42"
    roles = [m.get("role") for m in terminal.run.shared.get("messages", [])]
    assert roles == ["assistant", "tool", "assistant"]
