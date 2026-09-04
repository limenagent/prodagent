"""图与调度器：顺序、菱形并发、条件分支、回边循环、动态扇出汇聚、停滞与空转。"""

import pytest

from src.kernel import (
    FnBody,
    Node,
    Outcome,
    Plan,
    RunState,
    Scheduler,
    Send,
    add,
    append,
    last,
)


async def run_plan(plan, task="t"):
    return await Scheduler().run(plan, task=task)


# —— 顺序执行 ——
async def test_linear():
    p = Plan(channels={"log": append()})
    p.add(
        Node("a", FnBody(lambda x, ctx: Outcome.ok("A", log=["a"]))),
        Node("b", FnBody(lambda x, ctx: Outcome.ok(x + "B", log=["b"]))),
        Node("c", FnBody(lambda x, ctx: Outcome.ok(x + "C")), terminal=True),
    )
    p.edge("a", "b")
    p.edge("b", "c")
    run = await run_plan(p)
    assert run.state == RunState.COMPLETED
    assert run.final_output == "ABC"
    assert run.shared["log"] == ["a", "b"]
    assert run.metrics["waves"] == 3


# —— 菱形并发：append 不丢、add 求和 ——
async def test_diamond_parallel_reducers():
    p = Plan(channels={"items": append(), "n": add(0)})
    p.add(
        Node("a", FnBody(lambda x, ctx: Outcome.ok(None, items=["a"], n=1))),
        Node("b", FnBody(lambda x, ctx: Outcome.ok("B", items=["b"], n=1))),
        Node("c", FnBody(lambda x, ctx: Outcome.ok("C", items=["c"], n=1))),
        Node("d", FnBody(lambda x, ctx: Outcome.ok(sorted(ctx.shared["items"]))), terminal=True),
    )
    p.edge("a", "b")
    p.edge("a", "c")
    p.edge("b", "d")
    p.edge("c", "d")
    run = await run_plan(p)
    assert run.state == RunState.COMPLETED
    assert run.final_output == ["a", "b", "c"]
    assert run.shared["n"] == 3


# —— 条件分支：走左支，右支被结构性跳过 ——
async def test_conditional_branch_skips_dead_path():
    p = Plan(channels={"go_left": last(False)})
    p.add(
        Node("start", FnBody(lambda x, ctx: Outcome.ok(None, go_left=True))),
        Node("left", FnBody(lambda x, ctx: Outcome.ok("L")), terminal=True),
        Node("right", FnBody(lambda x, ctx: Outcome.ok("R")), terminal=True),
    )
    p.edge("start", "left", when=lambda s: s["go_left"])
    p.edge("start", "right", when=lambda s: not s["go_left"])
    run = await run_plan(p)
    assert run.final_output == "L"
    assert run.state_of("right").status.value == "skipped"


# —— 回边循环（Goto 重置目标）——
async def test_goto_loop():
    p = Plan(channels={"c": add(0)})

    def tick(x, ctx):
        return Outcome.goto("tick", c=1) if ctx.shared["c"] < 2 else Outcome.ok("stop", c=1)

    p.add(Node("tick", FnBody(tick), terminal=True))
    run = await run_plan(p)
    assert run.final_output == "stop"
    assert run.shared["c"] == 3


# —— 动态扇出 + 汇聚 ——
async def test_dynamic_fanout_fanin():
    p = Plan(channels={"outs": append()})
    p.add(
        Node(
            "fan",
            FnBody(
                lambda x, ctx: Outcome.fan_out(
                    Send("worker", 1, key="w1"), Send("worker", 2, key="w2")
                )
            ),
        ),
        Node("worker", FnBody(lambda x, ctx: Outcome.ok(None, outs=[x * 10])), template=True),
        Node("join", FnBody(lambda x, ctx: Outcome.ok(sorted(ctx.shared["outs"]))), terminal=True),
    )
    p.edge("worker", "join")
    run = await run_plan(p)
    assert run.final_output == [10, 20]
    assert sorted(run.instances["worker"]) == ["worker#w1", "worker#w2"]


# —— 无入口的环：互相等待、谁都起不来，要明确失败而不是静默成功 ——
async def test_stagnation_fails():
    p = Plan()
    p.add(
        Node("a", FnBody(lambda x, ctx: Outcome.ok("A"))),
        Node("b", FnBody(lambda x, ctx: Outcome.ok("B"))),
    )
    p.edge("a", "b")
    p.edge("b", "a")  # a 等 b、b 等 a，且没有入口节点
    run = await run_plan(p)
    assert run.state == RunState.FAILED
    assert "停滞" in (run.final_output or "")


# —— 只回边不前进，撞上 max_waves 防空转 ——
async def test_max_waves_guards_spin():
    p = Plan(channels={"c": add(0)})
    p.add(Node("spin", FnBody(lambda x, ctx: Outcome.goto("spin", c=1)), terminal=True))
    sch = Scheduler(max_waves=5)
    run = await sch.run(p)
    assert run.state == RunState.FAILED
    assert "最大波次" in run.final_output


# —— 状态机：非法转移直接拒绝 ——
async def test_illegal_transition_rejected():
    p = Plan()
    p.add(Node("a", FnBody(lambda x, ctx: Outcome.ok("A")), terminal=True))
    run = await run_plan(p)
    with pytest.raises(RuntimeError):
        run.complete("again")  # 已完成不能再完成
