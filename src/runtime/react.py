"""react —— 配方一：用内核原语拼出 ReAct。

内核里没有 ReAct。这里用三个节点 + 两条条件边 + 一条回边把它拼出来：

    think ──有工具调用?──▶ tools ──Goto 回边──▶ think
      │
      └──没有工具调用、已有答案?──▶ final

上下文压缩、长期记忆都是**可选注入的策略**：传进来就在 think 前生效，
不传也完全能跑——内核与这份配方都不依赖它们的具体实现。
"""

from __future__ import annotations

from typing import Any

from src.kernel import (
    FnBody,
    Goto,
    Node,
    Outcome,
    Plan,
    Run,
    append,
    last,
)


def _last_user_text(messages: list[dict]) -> str:
    for m in reversed(messages):
        if m.get("role") == "user":
            return str(m.get("content", ""))
    return ""


def build_react_plan(
    tools: Any, *, system: str = "", context: Any = None, memory: Any = None
) -> Plan:
    """tools 是满足 ToolPort 的工具注册表（见 runtime.tools.ToolRegistry）。"""

    async def think(_input, ctx):
        messages = list(ctx.shared["messages"])
        if context is not None:  # 策略：上下文窗口管理/压缩
            messages = await context.assemble(messages)
        sys_text = system
        if memory is not None:  # 策略：长期记忆检索后注入
            recalled = await memory.recall(_last_user_text(messages))
            if recalled:
                sys_text = (system + "\n\n" if system else "") + "相关记忆：\n" + recalled

        async def _delta(piece, kind="content"):  # 模型吐字：实时投到总线（不入事件日志）
            await ctx.emit("llm_delta", text=piece, kind=kind)

        reply = await ctx.llm_chat(
            messages, tools=tools.schemas(), system=sys_text or None, on_delta=_delta
        )
        if reply.tool_calls:
            # 要调工具：用 Goto 显式让 tools 重新就绪（多轮调用时它会被反复重入）。
            return Outcome(
                state_delta={
                    "messages": [{"role": "assistant", "tool_calls": reply.tool_calls}],
                    "pending": list(reply.tool_calls),
                },
                control=Goto("tools"),
            )
        return Outcome(
            state_delta={
                "messages": [{"role": "assistant", "text": reply.text}],
                "answer": reply.text,
            }
        )

    async def run_tools(_input, ctx):
        outputs = []
        for call in ctx.shared["pending"]:
            result = await ctx.call_tool(call.name, call.arguments)
            content = result.output if result.ok else f"[工具报错] {result.error}"
            outputs.append({"role": "tool", "name": call.name, "content": content})
        # 清空待办，并用 Goto 让 think 重新就绪——ReAct 的“循环”就是这条回边。
        return Outcome.goto("think", messages=outputs, pending=[])

    plan = Plan(channels={"messages": append(), "pending": last(None), "answer": last(None)})
    plan.add(
        Node("think", FnBody(think)),
        Node("tools", FnBody(run_tools)),
        Node("final", FnBody(lambda x, ctx: Outcome.ok(ctx.shared["answer"])), terminal=True),
    )
    # think⇄tools 构成环：
    # - think→tools 是条件边（pending 非空才走），保证“直接出答案”时不会误激活工具；
    # - 多轮调用时 tools 已完成，靠 think 返回的 Goto("tools") 把它重新置为就绪；
    # - tools 完用 Goto 回 think；只有出答案这一条条件边通向 final。
    plan.edge("think", "tools", when=lambda s: bool(s.get("pending")))
    plan.edge("tools", "think")
    plan.edge("think", "final", when=lambda s: bool(s.get("answer")))
    plan.entry = ("think",)
    return plan


def start_react_run(plan: Plan, task: str, history: list | None = None) -> Run:
    """创建一次 ReAct 运行并放入本轮用户消息（随后交给 Scheduler.drive）。

    history 是之前的对话消息：传入即多轮续聊（新 Run 接着旧上下文想），
    不传就是全新对话——会话状态由调用方持有，Agent 自身保持无状态。
    """
    run = Run.start(plan, task=task)
    run.shared["messages"] = (
        [*history, {"role": "user", "content": task}]
        if history
        else [{"role": "user", "content": task}]
    )
    return run
