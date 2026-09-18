"""Assemble a ReAct out of kernel primitives (run: PYTHONPATH=. python examples/react_demo.py).

The kernel contains no ReAct and no loop pattern. With three nodes, two
conditional edges, and one back edge, "think -> call tool -> feed the result
back -> think again -> answer" is assembled:

    user ─▶ think ──has tool calls?──▶ tools ──Goto back edge──▶ think
              │
              └──plain text answer?──▶ final

A single append-only ``messages`` channel holds the whole conversation; whether
to call tools or to finish is read from the shape of the last assistant message,
so no separate "pending"/"answer" state is needed.

The model and the tools are scripted fakes, so the whole example runs offline
and deterministically, no API key needed.
"""

import asyncio

from src.kernel import (
    FnBody,
    Goto,
    LlmReply,
    Node,
    Outcome,
    Plan,
    Scheduler,
    ToolCall,
    ToolResult,
    append,
)


def _last_assistant(state):
    # Routing is a pure function of the conversation: the last assistant message
    # either requests tool calls or carries the final text.
    messages = state.get("messages") or []
    return messages[-1] if messages and messages[-1].get("role") == "assistant" else {}


class FakeLlm:
    """A scripted model: the first turn asks for the weather; the second turn
    (after seeing the tool result) gives the final answer."""

    def __init__(self):
        self.n = 0

    async def chat(self, messages, *, tools=None, system=None, on_delta=None):
        self.n += 1
        if self.n == 1:
            return LlmReply(tool_calls=[ToolCall("get_weather", {"city": "Beijing"})], tokens=12)
        return LlmReply(text="Beijing is sunny today, 26°C.", tokens=8)


class FakeTools:
    async def dispatch(self, call: ToolCall, ctx=None) -> ToolResult:
        if call.name == "get_weather":
            return ToolResult.success(f"{call.arguments['city']}: sunny, 26°C", call.call_id)
        return ToolResult.failure("unknown tool", call.call_id)


async def think(_input, ctx):
    reply = await ctx.llm_chat(ctx.shared["messages"])
    if reply.tool_calls:
        # Record the assistant message, then Goto tools: on a multi-round loop a
        # completed node is re-entered only via Goto, never via a static edge.
        return Outcome(
            state_delta={"messages": [{"role": "assistant", "tool_calls": reply.tool_calls}]},
            control=Goto("tools"),
        )
    # No tool calls = the final answer is out; the static conditional edge
    # routes to final on exactly this message shape.
    return Outcome(state_delta={"messages": [{"role": "assistant", "text": reply.text}]})


async def tools(_input, ctx):
    results = []
    for call in _last_assistant(ctx.shared).get("tool_calls", []):
        r = await ctx.call_tool(call.name, call.arguments)
        results.append({"role": "tool", "name": call.name, "content": r.output})
    # Append the tool results and Goto think — that Goto is the back edge.
    return Outcome.goto("think", messages=results)


def build_react_plan() -> Plan:
    p = Plan(channels={"messages": append()})
    p.add(
        Node("think", FnBody(think)),
        Node("tools", FnBody(tools)),
        Node(
            "final",
            FnBody(lambda x, ctx: Outcome.ok(_last_assistant(ctx.shared).get("text"))),
            terminal=True,
        ),
    )
    # think→tools / think→final are conditional on the last assistant message;
    # after tools, a Goto back to think drives each new round.
    p.edge("think", "tools", when=lambda s: bool(_last_assistant(s).get("tool_calls")))
    p.edge("tools", "think")
    p.edge(
        "think",
        "final",
        when=lambda s: bool(_last_assistant(s)) and not _last_assistant(s).get("tool_calls"),
    )
    p.entry = ("think",)
    return p


async def main():
    from src.kernel import Run

    plan = build_react_plan()
    sch = Scheduler(llm=FakeLlm(), tools=FakeTools())
    run = Run.start(plan, task="What's the weather in Beijing?")
    run.shared["messages"] = [{"role": "user", "content": run.task}]  # first-turn user input
    await sch.drive(plan, run)
    print("Final answer:", run.final_output)
    print(
        "waves:",
        run.metrics["waves"],
        "| LLM calls:",
        run.metrics["llm_calls"],
        "| tool calls:",
        run.metrics["tool_calls"],
    )


if __name__ == "__main__":
    asyncio.run(main())
