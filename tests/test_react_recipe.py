"""用内核原语拼出 ReAct：内核没有任何模式枚举，think⇄tools 只是图。"""

from src.kernel import Run, RunState, Scheduler
from examples.react_demo import FakeLlm, FakeTools, build_react_plan


async def test_react_is_assembled_from_primitives():
    plan = build_react_plan()
    llm, tools = FakeLlm(), FakeTools()
    sch = Scheduler(llm=llm, tools=tools)
    run = Run.start(plan, task="北京天气怎么样？")
    run.shared["messages"] = [{"role": "user", "content": run.task}]
    await sch.drive(plan, run)

    assert run.state == RunState.COMPLETED
    assert run.final_output == "北京今天晴，26℃。"
    # think -> tools -> think -> final
    assert run.metrics["waves"] == 4
    assert run.metrics["llm_calls"] == 2
    assert run.metrics["tool_calls"] == 1
    # 工具结果被喂回了消息历史，模型正是据此给出最终答案。
    assert any(m.get("role") == "tool" for m in run.shared["messages"])
