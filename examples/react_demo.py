"""用内核原语“拼”出一个 ReAct（运行：PYTHONPATH=. python examples/react_demo.py）。

内核里没有任何 ReAct/循环模式。这里用两个节点 + 条件边 + 一条回边，
就把“思考 -> 调工具 -> 把结果喂回 -> 再思考 -> 出答案”拼了出来：

    user ─▶ think ──有工具调用?──▶ tools ──Goto 回边──▶ think
              │
              └──没有工具调用、已有答案?──▶ final

模型和工具都是脚本化的 Fake，所以整个例子离线、确定性地跑，不需要 API key。
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
    """脚本化模型：第一次要求查天气，第二次（看到工具结果后）给最终答案。"""

    def __init__(self):
        self.n = 0

    async def chat(self, messages, *, tools=None, system=None, on_delta=None):
        self.n += 1
        if self.n == 1:
            return LlmReply(tool_calls=[ToolCall("get_weather", {"city": "北京"})], tokens=12)
        return LlmReply(text="北京今天晴，26℃。", tokens=8)


class FakeTools:
    async def dispatch(self, call: ToolCall, ctx=None) -> ToolResult:
        if call.name == "get_weather":
            return ToolResult.success(f"{call.arguments['city']} 晴 26℃", call.call_id)
        return ToolResult.failure("unknown tool", call.call_id)


async def think(_input, ctx):
    reply = await ctx.llm_chat(ctx.shared["messages"])
    if reply.tool_calls:
        # 要调工具：Goto 让 tools 就绪（多轮时它会被反复重入），并记下这一步。
        return Outcome(
            state_delta={"messages": [{"role": "assistant", "calls": reply.tool_calls}],
                         "pending": reply.tool_calls},
            control=Goto("tools"))
    # 没有工具调用 = 出最终答案，静态条件边据此走向 final。
    return Outcome(state_delta={"messages": [{"role": "assistant", "text": reply.text}],
                                "answer": reply.text})


async def tools(_input, ctx):
    results = []
    for call in ctx.shared["pending"]:
        r = await ctx.call_tool(call.name, call.arguments)
        results.append({"role": "tool", "name": call.name, "content": r.output})
    # 清空待办，并通过 Goto 让 think 重新就绪——回边就是这么来的。
    return Outcome.goto("think", messages=results, pending=[])


def build_react_plan() -> Plan:
    p = Plan(channels={"messages": append(), "pending": last(None), "answer": last(None)})
    p.add(Node("think", FnBody(think)),
          Node("tools", FnBody(tools)),
          Node("final", FnBody(lambda x, ctx: Outcome.ok(ctx.shared["answer"])), terminal=True))
    # think→tools 条件边（有待办才走），多轮重入靠 think 的 Goto；tools 完 Goto 回 think。
    p.edge("think", "tools", when=lambda s: bool(s.get("pending")))
    p.edge("tools", "think")
    p.edge("think", "final", when=lambda s: bool(s.get("answer")))
    p.entry = ("think",)
    return p


async def main():
    from src.kernel import Run
    plan = build_react_plan()
    sch = Scheduler(llm=FakeLlm(), tools=FakeTools())
    run = Run.start(plan, task="北京天气怎么样？")
    run.shared["messages"] = [{"role": "user", "content": run.task}]  # 首轮用户输入
    await sch.drive(plan, run)
    print("最终答案：", run.final_output)
    print("波次：", run.metrics["waves"], "| 模型调用：", run.metrics["llm_calls"],
          "| 工具调用：", run.metrics["tool_calls"])


if __name__ == "__main__":
    asyncio.run(main())
