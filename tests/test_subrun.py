"""多 Agent：子 Agent 是某个 body 递归跑起的子 Run，用的还是同一个内核。"""

from src.kernel import (
    FnBody,
    Node,
    Outcome,
    Plan,
    RunState,
    Scheduler,
    SubPlanBody,
    append,
)


def child_plan():
    c = Plan(channels={"trace": append()})
    c.add(Node("work", FnBody(lambda x, ctx: Outcome.ok("child-result", trace=[x])), terminal=True))
    return c


async def test_subrun_call_returns_output_to_parent():
    parent = Plan()
    parent.add(Node("delegate", SubPlanBody(child_plan()), terminal=True))
    sch = Scheduler()
    started = []
    sch.bus.on("run_started", lambda evt: started.append(evt))
    run = await sch.run(parent, task="给子Agent的任务")
    assert run.state == RunState.COMPLETED
    # call 语义：默认只把子 Run 的最终产出交回父节点。
    assert run.final_output == "child-result"
    # 子 Run 的 run_started 事件带着 parent_id，Run 树可重建。
    child_starts = [e for e in started if e.parent_id == run.run_id]
    assert len(child_starts) == 1


async def test_nested_subruns_build_a_run_tree():
    # 祖父 -> 父(body 是子 Run) -> 子，逐层只回传 output。
    grandchild = child_plan()
    middle = Plan()
    middle.add(Node("m", SubPlanBody(grandchild), terminal=True))
    root = Plan()
    root.add(Node("r", SubPlanBody(middle), terminal=True))
    run = await Scheduler().run(root, task="deep")
    assert run.state == RunState.COMPLETED
    assert run.final_output == "child-result"
