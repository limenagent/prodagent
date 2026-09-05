"""门面层测试：Agent / Workflow 好用的高层 API，底层仍是同一套 BSP 内核。"""

from src import Agent, Workflow, go, send, wait_human
from src.kernel import ToolCall, append
from src.runtime.llm import ScriptedLlm


async def test_agent_plain_answer_without_tools():
    agent = Agent("chat", model=ScriptedLlm(["直接回答"]))
    result = await agent.run("你好")
    assert result.output == "直接回答"
    assert result.status == "completed"


async def test_agent_multi_turn_tools():
    async def search(query, ctx):
        return f"结果:{query}"

    agent = Agent(
        "searcher",
        model=ScriptedLlm(
            [
                ToolCall("search", {"query": "a"}),
                ToolCall("search", {"query": "b"}),
                "综合 a、b 的结论",
            ]
        ),
        tools=[search],
    )
    result = await agent.run("查两个")
    assert result.output == "综合 a、b 的结论"
    assert result.metrics["tool_calls"] == 2


async def test_teammates_run_with_their_own_models():
    # 主管派活给两个队友；每个队友用自己的模型，不会消耗主管的脚本。
    researcher = Agent("researcher", model=ScriptedLlm(["资料 X"]))
    writer = Agent("writer", model=ScriptedLlm(["成稿 Y"]))
    boss = Agent(
        "boss",
        model=ScriptedLlm(
            [
                ToolCall("researcher", {"task": "去查"}),
                ToolCall("writer", {"task": "去写"}),
                "汇总完成",
            ]
        ),
        teammates=[researcher, writer],
    )
    result = await boss.run("做个课题")
    assert result.output == "汇总完成"
    assert result.metrics["tool_calls"] == 2


async def test_workflow_static_graph_and_auto_state():
    wf = Workflow()
    wf.add("a", lambda x, ctx: {"v": 1})
    wf.add("b", lambda x, ctx: go("c", v=ctx.shared["v"] + 10))
    wf.add("c", lambda x, ctx: ctx.shared["v"], terminal=True)
    wf.edge("a", "b").edge("b", "c").entry("a")
    result = await wf.run()
    assert result.output == 11  # 未声明的 v 自动补了 last 通道


async def test_workflow_branch():
    wf = Workflow()
    wf.add("decide", lambda x, ctx: {"kind": x})
    wf.add("yes", lambda x, ctx: "走了 yes", terminal=True)
    wf.add("no", lambda x, ctx: "走了 no", terminal=True)
    wf.edge("decide", "yes", when=lambda s: s["kind"] == "yes")
    wf.edge("decide", "no", when=lambda s: s["kind"] == "no")
    wf.entry("decide")
    assert (await wf.run("yes")).output == "走了 yes"
    assert (await wf.run("no")).output == "走了 no"


async def test_workflow_dynamic_fan_out():
    wf = Workflow()
    wf.channel("logs", append())

    async def dispatch(x, ctx):
        # 要几份就发几个 Send（数量运行时才知道），引擎把它们放进同一波并行。
        return [send("worker", {"i": i}) for i in (1, 2, 3)]

    async def worker(item, ctx):
        return {"logs": [item["i"] * 10]}

    async def merge(x, ctx):
        return sorted(ctx.shared["logs"])

    wf.add("dispatch", dispatch)
    wf.add("worker", worker, template=True)
    wf.add("merge", merge, terminal=True, join="all")
    wf.edge("dispatch", "worker")
    wf.edge("worker", "merge")
    wf.entry("dispatch")
    assert (await wf.run()).output == [10, 20, 30]


async def test_workflow_agent_as_node():
    worker = Agent("worker", model=ScriptedLlm(["子 Agent 结果"]))
    wf = Workflow()
    wf.add("call_agent", worker, terminal=True)  # 节点直接放 Agent，自包含跑
    wf.entry("call_agent")
    assert (await wf.run("任务")).output == "子 Agent 结果"


async def test_workflow_wait_human_and_resume():
    wf = Workflow()

    async def approve(x, ctx):
        if ctx.resume_value is None:
            return wait_human("确认执行吗？", {"amount": 100})
        return f"按你的选择执行：{ctx.resume_value}"

    wf.add("approve", approve, terminal=True)
    wf.entry("approve")

    first = await wf.run("付款")
    assert first.status == "suspended"
    second = await wf.resume(first.run_id, {"approved": True})
    assert second.output == "按你的选择执行：{'approved': True}"


async def test_workflow_goto_agent_is_transfer():
    # 交接（transfer）= 同图里 go 到另一个 Agent 节点、且不画回边：
    # relay 把输入转交给 repairer，repairer 用自己的模型接手并直接收尾。
    repairer = Agent("repairer", model=ScriptedLlm(["已扩容，恢复"]))
    wf = Workflow()
    wf.add("relay", lambda x, ctx: go("repairer", x))
    wf.add("repairer", repairer, terminal=True)
    wf.entry("relay")
    result = await wf.run("故障=连接池耗尽")
    assert result.output == "已扩容，恢复"  # 没有回边，控制权一去不返
