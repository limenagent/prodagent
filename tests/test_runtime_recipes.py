"""Recipe-layer tests: ReAct multi-round tools, tool governance, and the two
public composition faces — Workflow (static graph) and Agent.sub_agents (runtime
delegation). Every multi-agent/pattern test drives the public API, so the tests
double as usage docs and never depend on an internal plan-builder.
"""

from src import Agent, Workflow, send
from src.kernel import FnBody, Node, Outcome, Plan, Scheduler, ToolCall
from src.runtime.llm import ScriptedLlm
from src.runtime.react import build_react_plan, start_react_run
from src.runtime.tools import ToolRegistry


async def test_react_multiple_tool_rounds():
    # the answer only comes after two tool calls, verifying a cycle node can be re-entered via Goto repeatedly.
    reg = ToolRegistry()

    async def search(query, ctx):
        return f"结果({query})"

    reg.function(search, description="搜索")

    llm = ScriptedLlm(
        [
            ToolCall("search", {"query": "一"}),
            ToolCall("search", {"query": "二"}),
            "综合答复",
        ]
    )
    plan = build_react_plan(reg)
    # routing is derived from the last assistant message, so the recipe stores
    # only the history — no mirrored pending/answer slots to keep in sync.
    assert set(plan.channels) == {"messages"}
    sch = Scheduler(llm=llm, tools=reg)
    run = start_react_run(plan, "查两轮")
    await sch.drive(plan, run)
    assert run.final_output == "综合答复"
    assert run.metrics["tool_calls"] == 2
    assert run.metrics["llm_calls"] == 3


async def test_tool_schema_and_missing_arg_feedback():
    reg = ToolRegistry()

    async def add_numbers(a, b, ctx):
        return a + b

    reg.function(add_numbers, description="相加")

    schema = reg.schemas()[0]["function"]
    assert set(schema["parameters"]["required"]) == {"a", "b"}  # ctx does not enter the schema

    # Missing argument: returns failure feedback rather than raising.
    result = await reg.dispatch(ToolCall("add_numbers", {"a": 1}))
    assert not result.ok and "required" in result.error


async def test_write_tool_goes_through_approval_gate():
    reg = ToolRegistry()

    async def refund(order_id, ctx):
        return f"已退 {order_id}"

    reg.function(refund, side_effect="write")

    # Approval gate rejects: the write is blocked and the reason is fed back to the model.
    sch = Scheduler(tools=reg)
    reg.bus = sch.bus  # wire the registry to the same bus to reach the adjudication gate
    sch.bus.checker("tool:refund", lambda **_: False)
    result = await reg.dispatch(ToolCall("refund", {"order_id": "o1"}), ctx=None)
    assert not result.ok and "approved" in result.error


async def test_pipeline_runs_in_order():
    wf = Workflow()

    async def a(x, ctx):
        return f"甲:{x}"

    async def b(x, ctx):
        return f"乙:{x}"

    wf.add_node("a", a)
    wf.add_node("b", b, terminal=True)
    wf.add_edge("a", "b")
    wf.entry("a")
    r = await wf.run("输入")
    assert r.output == "乙:甲:输入"


async def test_plan_first_fan_out():
    wf = Workflow()

    async def planner(task, ctx):
        steps = [{"id": "s1", "instruction": "A"}, {"id": "s2", "instruction": "B"}]
        return [send("worker", step, key=step["id"]) for step in steps]

    async def worker(step, ctx):
        return step["instruction"]

    wf.add_node("planner", planner)
    wf.add_node("worker", worker, template=True)
    wf.add_node("synth", lambda x, ctx: Outcome.ok(x), terminal=True)
    wf.add_edge("worker", "synth")
    wf.entry("planner")
    r = await wf.run("拆两步")
    # the template predecessor's instances are aggregated into the join point
    assert sorted(r.output) == ["A", "B"]


async def test_supervisor_delegates_to_workers():
    researcher = Agent("researcher", model=ScriptedLlm(["调研结果"]), instruction="查资料")
    writer = Agent("writer", model=ScriptedLlm(["写稿结果"]), instruction="写稿")
    sup = Agent(
        "sup",
        model=ScriptedLlm(
            [
                ToolCall("researcher", {"task": "查 X"}),
                ToolCall("writer", {"task": "写 Y"}),
                "汇总完成",
            ]
        ),
        instruction="主管，自己不执行",
        sub_agents=[researcher, writer],
    )
    r = await sup.run("做课题")
    assert r.output == "汇总完成"
    assert (
        r.metrics["tool_calls"] == 2
    )  # the two delegations, each specialist's own think underneath


async def test_goto_carries_payload_to_target():
    # the payload a Goto transition carries becomes the target node's input this time (a handoff carries a summary this way).
    plan = Plan()
    plan.add(Node("a", FnBody(lambda x, ctx: Outcome.goto("b", "交接物"))))
    plan.add(Node("b", FnBody(lambda x, ctx: Outcome.ok(f"b收到:{x}")), terminal=True))
    sch = Scheduler()
    run = await sch.run(plan, task="起点")
    assert run.final_output == "b收到:交接物"
    # b has no static incoming edge, it was activated by Goto; with no back-edge, a never runs a second time.
    assert run.state_of("a").attempts == 1


async def test_supervisor_worker_runs_are_named():
    researcher = Agent("researcher", model=ScriptedLlm(["调研结果"]), instruction="查资料")
    sup = Agent(
        "sup",
        model=ScriptedLlm([ToolCall("researcher", {"task": "查 X"}), "汇总完成"]),
        instruction="主管",
        sub_agents=[researcher],
    )
    sch = sup.host()  # host first, then observe the live stream
    started = []
    sch.bus.on("run_started", lambda evt: started.append(evt))

    r = await sup.run("做课题")
    assert r.output == "汇总完成"
    # A sub-agent is an Agent with its own name, and the spawned worker Run really
    # carries it on run_started — not just the field, the observable event.
    names = [e.data.get("name") for e in started]
    assert "researcher" in names
