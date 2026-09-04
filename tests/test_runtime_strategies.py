"""策略层测试：上下文压缩、记忆、技能、MCP 归一、背压、文件级断点续跑。"""

from src.backends.file_store import FileCheckpointStore
from src.kernel import (
    Bus,
    FnBody,
    Node,
    Outcome,
    Plan,
    Scheduler,
    ToolCall,
)
from src.runtime.context import SummarizingContext, WindowContext
from src.runtime.llm import ScriptedLlm
from src.runtime.mcp import InProcessMCPServer, load_mcp_tools
from src.runtime.memory import InMemoryMemory
from src.runtime.skills import Skill, SkillRegistry
from src.runtime.tools import ToolRegistry


async def test_window_context_keeps_head_and_tail():
    ctx = WindowContext(keep_last=2)
    msgs = [{"role": "user", "content": "首问"}] + [
        {"role": "assistant", "content": str(i)} for i in range(6)
    ]
    out = await ctx.assemble(msgs)
    assert out[0]["content"] == "首问"
    assert [m["content"] for m in out[1:]] == ["4", "5"]


async def test_summarizing_context_compresses_old_messages():
    llm = ScriptedLlm(["旧消息要点：X、Y"])
    ctx = SummarizingContext(llm, max_messages=3, keep_last=2)
    msgs = [{"role": "user", "content": f"m{i}"} for i in range(6)]
    out = await ctx.assemble(msgs)
    assert out[0]["role"] == "system" and "X、Y" in out[0]["content"]
    assert len(out) == 3  # 一条摘要 + 最近两条


async def test_memory_remember_and_recall():
    mem = InMemoryMemory()
    await mem.remember("用户对芒果过敏", tags=["偏好"])
    await mem.remember("订单 123 已发货", tags=["订单"])
    text = await mem.recall("我对什么过敏？")
    assert "芒果" in text


async def test_skill_match_and_apply():
    reg = SkillRegistry()
    reg.register(Skill("退款流程", "处理退款退货售后", instructions="先核验订单再退款"))
    skill = reg.match("这个客户要退款")
    assert skill and skill.name == "退款流程"
    system = reg.apply_to_system(skill, "你是客服")
    assert "先核验订单" in system


async def test_mcp_tools_share_same_pipeline():
    server = InProcessMCPServer("demo")
    server.define(
        "echo",
        lambda args: args["x"],
        description="回显",
        parameters={"type": "object", "properties": {"x": {"type": "integer"}}},
    )
    reg = ToolRegistry()
    names = await load_mcp_tools(reg, server)
    assert names == ["echo"]
    result = await reg.dispatch(ToolCall("echo", {"x": 42}))
    assert result.ok and result.output == 42  # MCP 工具与本地函数走同一条路


async def test_bus_backpressure_drop():
    bus = Bus()
    sub = bus.subscribe("tick", maxsize=1, on_full="drop")
    for i in range(5):
        await bus.fire("tick", i=i)  # 没人消费，队列满后丢帧不阻塞
    assert sub.dropped == 4
    first = await sub.get()
    assert first["i"] == 0


async def test_file_store_resume_across_schedulers(tmp_path):
    # 模拟进程重启：两个 Scheduler 实例共享同一个文件目录，挂起后能接着跑。
    def build():
        p = Plan()

        def approve(_x, ctx):
            if ctx.resume_value is None:
                return Outcome.park("approval", question="确认？")
            return Outcome.ok(ctx.resume_value)

        p.add(Node("approve", FnBody(approve), terminal=True))
        return p

    store = FileCheckpointStore(str(tmp_path))
    sch1 = Scheduler(store=store)
    run = await sch1.run(build())
    assert str(run.state) == "suspended"

    sch2 = Scheduler(store=store)
    run2 = await sch2.resume(build(), run.run_id, {"ok": True})
    assert str(run2.state) == "completed"
    assert run2.final_output == {"ok": True}
