"""Assemble a ReAct out of kernel primitives (run: PYTHONPATH=. python examples/react_demo.py).

The kernel contains no ReAct and no loop pattern. With two nodes, a
conditional edge, and one back edge, "think -> call tool -> feed the result
back -> think again -> answer" is assembled:

    user ─▶ think ──has tool calls?──▶ tools ──Goto back edge──▶ think
              │
              └──no tool calls, answer in hand?──▶ final

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
    last,
)


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
        # Tool calls wanted: Goto makes tools ready (on multi-round loops it
        # gets re-entered repeatedly), and this step is recorded.
        return Outcome(
            state_delta={
                "messages": [{"role": "assistant", "calls": reply.tool_calls}],
                "pending": reply.tool_calls,
            },
            control=Goto("tools"),
        )
    # No tool calls = the final answer is out; the static conditional edge
    # routes to final on exactly this.
    return Outcome(
        state_delta={"messages": [{"role": "assistant", "text": reply.text}], "answer": reply.text}
    )


async def tools(_input, ctx):
    results = []
    for call in ctx.shared["pending"]:
        r = await ctx.call_tool(call.name, call.arguments)
        results.append({"role": "tool", "name": call.name, "content": r.output})
    # Clear the pending list and Goto think back to ready — that is where the
    # back edge comes from.
    return Outcome.goto("think", messages=results, pending=[])


def build_react_plan() -> Plan:
    p = Plan(channels={"messages": append(), "pending": last(None), "answer": last(None)})
    p.add(
        Node("think", FnBody(think)),
        Node("tools", FnBody(tools)),
        Node("final", FnBody(lambda x, ctx: Outcome.ok(ctx.shared["answer"])), terminal=True),
    )
    # think→tools is a conditional edge (taken only with pending items);
    # multi-round re-entry rides on think's Goto; after tools, Goto back to think.
    p.edge("think", "tools", when=lambda s: bool(s.get("pending")))
    p.edge("tools", "think")
    p.edge("think", "final", when=lambda s: bool(s.get("answer")))
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
