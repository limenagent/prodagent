"""第一个例子：不接任何模型，只用纯函数节点看清“波次”是怎么推进的。

运行：PYTHONPATH=. python examples/graph_demo.py

图是一个菱形：
        a
       / \\
      b   c   （b、c 互不依赖，在同一波并发执行）
       \\ /
        d
订阅总线事件，就能看到调度器每一波让谁就绪、谁完成——这就是 BSP 超步。
"""

import asyncio

from src.kernel import (
    FnBody,
    Node,
    Outcome,
    Plan,
    Scheduler,
    add,
    append,
)


def build_plan() -> Plan:
    p = Plan(channels={"log": append(), "cost": add(0)})
    p.add(
        Node("a", FnBody(lambda x, ctx: Outcome.ok("来自a", log=["a 跑了"], cost=1))),
        Node("b", FnBody(lambda x, ctx: Outcome.ok(f"b 收到：{x}", log=["b 跑了"], cost=2))),
        Node("c", FnBody(lambda x, ctx: Outcome.ok(f"c 收到：{x}", log=["c 跑了"], cost=3))),
        Node("d", FnBody(lambda x, ctx: Outcome.ok(x, log=["d 汇总"])), terminal=True),
    )
    p.edge("a", "b")
    p.edge("a", "c")
    p.edge("b", "d")
    p.edge("c", "d")
    return p


async def main():
    plan = build_plan()
    sch = Scheduler()

    # 订阅总线：旁观内核每一步，观察者出错也不会影响执行（第 21 课）。
    sch.bus.on("node_started", lambda evt: print(f"  ▶ 开始 {evt.data['node']}"))
    sch.bus.on("node_completed", lambda evt: print(f"  ✔ 完成 {evt.data['node']}"))

    print("运行菱形图：")
    run = await sch.run(plan, task="起点输入")
    print("最终结果：", run.final_output)
    print("共享状态：", run.shared)
    print("波次数：", run.metrics["waves"], "（a | b,c | d，正好三波）")


if __name__ == "__main__":
    asyncio.run(main())
