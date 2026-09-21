"""Facade-layer tests: Agent / Workflow's ergonomic high-level API, still backed by the same BSP kernel underneath."""

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
    # the boss delegates to two teammates; each uses its own model, without consuming the boss's script.
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
    wf.add_node("a", lambda x, ctx: {"v": 1})
    wf.add_node("b", lambda x, ctx: go("c", v=ctx.shared["v"] + 10))
    wf.add_node("c", lambda x, ctx: ctx.shared["v"], terminal=True)
    wf.add_edge("a", "b").add_edge("b", "c").entry("a")
    result = await wf.run()
    assert result.output == 11  # an undeclared v channel is auto-filled as a last channel


async def test_workflow_branch():
    wf = Workflow()
    wf.add_node("decide", lambda x, ctx: {"kind": x})
    wf.add_node("yes", lambda x, ctx: "走了 yes", terminal=True)
    wf.add_node("no", lambda x, ctx: "走了 no", terminal=True)
    wf.add_edge("decide", "yes", when=lambda s: s["kind"] == "yes")
    wf.add_edge("decide", "no", when=lambda s: s["kind"] == "no")
    wf.entry("decide")
    assert (await wf.run("yes")).output == "走了 yes"
    assert (await wf.run("no")).output == "走了 no"


async def test_workflow_dynamic_fan_out():
    wf = Workflow()
    wf.channel("logs", append())

    async def dispatch(x, ctx):
        # send one Send per item (the count is only known at runtime); the engine runs them all in one wave.
        return [send("worker", {"i": i}) for i in (1, 2, 3)]

    async def worker(item, ctx):
        return {"logs": [item["i"] * 10]}

    async def merge(x, ctx):
        return sorted(ctx.shared["logs"])

    wf.add_node("dispatch", dispatch)
    wf.add_node("worker", worker, template=True)
    wf.add_node("merge", merge, terminal=True, join="all")
    wf.add_edge("dispatch", "worker")
    wf.add_edge("worker", "merge")
    wf.entry("dispatch")
    assert (await wf.run()).output == [10, 20, 30]


async def test_workflow_agent_as_node():
    worker = Agent("worker", model=ScriptedLlm(["子 Agent 结果"]))
    wf = Workflow()
    wf.add_node(
        "call_agent", worker, terminal=True
    )  # a node can hold an Agent directly, runs self-contained
    wf.entry("call_agent")
    assert (await wf.run("任务")).output == "子 Agent 结果"


async def test_workflow_wait_human_and_resume():
    wf = Workflow()

    async def approve(x, ctx):
        if ctx.resume_value is None:
            return wait_human("确认执行吗？", {"amount": 100})
        return f"按你的选择执行：{ctx.resume_value}"

    wf.add_node("approve", approve, terminal=True)
    wf.entry("approve")

    first = await wf.run("付款")
    assert first.status == "suspended"
    second = await wf.resume(first.run_id, {"approved": True})
    assert second.output == "按你的选择执行：{'approved': True}"


async def test_workflow_goto_agent_is_transfer():
    # transfer = a go to another Agent node within the same graph, with no back-edge drawn:
    # relay hands its input to repairer, which takes over with its own model and finishes.
    repairer = Agent("repairer", model=ScriptedLlm(["已扩容，恢复"]))
    wf = Workflow()
    wf.add_node("relay", lambda x, ctx: go("repairer", x))
    wf.add_node("repairer", repairer, terminal=True)
    wf.entry("relay")
    result = await wf.run("故障=连接池耗尽")
    assert result.output == "已扩容，恢复"  # no back-edge, control never returns


async def test_teammate_events_flow_on_parent_bus_without_explicit_wiring():
    child = Agent("child", model=ScriptedLlm(["子任务完成"]), instruction="你是子专家。")
    parent = Agent(
        "parent",
        model=ScriptedLlm([ToolCall("child", {"task": "去办"}), "汇总完成"]),
        instruction="你是主管。",
        teammates=[child],
    )
    seen = []
    parent.bus.on("run_started", lambda evt: seen.append(("run_started", evt.data.get("name"))))
    parent.bus.on("node_started", lambda evt: seen.append(("node_started", evt.data.get("node"))))

    result = await parent.run("请委派")

    assert result.output == "汇总完成"
    # The teammate was never handed a bus: assembly injects the parent's, so
    # its run lands on the same observable stream, named after its blueprint.
    assert ("run_started", "child") in seen
    assert ("run_started", "parent") in seen
    # Node names stay clean — no agent prefix baked into the event.
    assert ("node_started", "think") in seen


async def test_teammate_bus_sharing_reaches_any_depth():
    grand = Agent("grand", model=ScriptedLlm(["孙完成"]), instruction="你是孙。")
    mid = Agent(
        "mid",
        model=ScriptedLlm([ToolCall("grand", {"task": "去"}), "子汇总"]),
        teammates=[grand],
    )
    top = Agent(
        "top",
        model=ScriptedLlm([ToolCall("mid", {"task": "去"}), "总汇总"]),
        teammates=[mid],
    )
    started, nodes = [], []
    top.bus.on("run_started", lambda evt: started.append(evt))
    top.bus.on("node_started", lambda evt: nodes.append(evt))

    result = await top.run("三层委派")

    assert result.output == "总汇总"
    names = [e.data.get("name") for e in started]
    assert "mid" in names and "grand" in names
    # The innermost run's node events land on the ROOT bus: one-level
    # injection would have left `grand` on mid's stale private bus.
    grand_ids = {e.run_id for e in started if e.data.get("name") == "grand"}
    assert any(e.run_id in grand_ids for e in nodes)
