"""配方层测试：ReAct 多轮工具、工具治理、plan-first、多 Agent 协作。"""

from src.kernel import (
    FnBody,
    Node,
    Outcome,
    Plan,
    Scheduler,
    ToolCall,
)
from src.runtime.llm import ScriptedLlm
from src.runtime.tools import ToolRegistry
from src.runtime.react import build_react_plan, start_react_run
from src.runtime.plan_first import build_plan_execute
from src.runtime.multiagent import (
    HandoffController,
    build_pipeline,
    build_supervisor,
)


def simple_plan(text):
    p = Plan()
    p.add(Node("n", FnBody(lambda x, ctx: Outcome.ok(f"{text}:{x}")), terminal=True))
    return p


async def test_react_multiple_tool_rounds():
    # 连续两次工具调用后才出答案，验证环上节点能被 Goto 反复重入。
    reg = ToolRegistry()

    async def search(query, ctx):
        return f"结果({query})"
    reg.function(search, description="搜索")

    llm = ScriptedLlm([
        ToolCall("search", {"query": "一"}),
        ToolCall("search", {"query": "二"}),
        "综合答复",
    ])
    plan = build_react_plan(reg)
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
    assert set(schema["parameters"]["required"]) == {"a", "b"}  # ctx 不进 schema

    # 缺参数：返回 failure 反馈，而不是抛异常。
    result = await reg.dispatch(ToolCall("add_numbers", {"a": 1}))
    assert not result.ok and "必填" in result.error


async def test_write_tool_goes_through_approval_gate():
    reg = ToolRegistry()

    async def refund(order_id, ctx):
        return f"已退 {order_id}"
    reg.function(refund, side_effect="write")

    # 审批门拒绝：写操作被拦下，原因可回喂给模型。
    sch = Scheduler(tools=reg)
    reg.bus = sch.bus                       # 工具注册表接上同一条总线才能过裁决门
    sch.bus.checker("tool:refund", lambda **_: False)
    result = await reg.dispatch(ToolCall("refund", {"order_id": "o1"}), ctx=None)
    assert not result.ok and "批准" in result.error


async def test_plan_first_fan_out():
    async def make_steps(task, ctx):
        return [{"id": "s1", "instruction": "A"}, {"id": "s2", "instruction": "B"}]
    worker = FnBody(lambda step, ctx: Outcome.ok(step["instruction"]))
    plan = build_plan_execute(make_steps=make_steps, worker=worker)
    run = await Scheduler().run(plan, task="拆两步")
    assert sorted(run.final_output) == ["A", "B"]


async def test_pipeline_runs_in_order():
    plan = build_pipeline([("a", simple_plan("甲")), ("b", simple_plan("乙"))])
    run = await Scheduler().run(plan, task="输入")
    assert run.final_output == "乙:甲:输入"


async def test_supervisor_delegates_to_workers():
    reg = ToolRegistry()
    workers = {"researcher": (simple_plan("调研"), "查资料"),
               "writer": (simple_plan("写作"), "写稿")}
    plan = build_supervisor(workers, registry=reg)
    llm = ScriptedLlm([
        ToolCall("researcher", {"task": "查 X"}),
        ToolCall("writer", {"task": "写 Y"}),
        "汇总完成",
    ])
    sch = Scheduler(llm=llm, tools=reg)
    run = start_react_run(plan, "做课题")
    await sch.drive(plan, run)
    assert run.final_output == "汇总完成"
    assert run.metrics["tool_calls"] == 2


async def test_handoff_transfers_without_returning():
    target = simple_plan("接棒")
    controller = HandoffController({"b": target})
    plan = Plan()
    plan.add(Node("a", FnBody(lambda x, ctx: Outcome.handoff("b", "交给你")), terminal=True))
    sch = Scheduler(on_handoff=controller)
    run = await sch.run(plan, task="起点")
    assert run.final_output == "接棒:交给你"          # 接力者的最终结果直接落到当前 Run
    assert controller.chain[0]["to"] == "b"          # 交接链可审计
