"""Interrupt：节点请求放手暂停，检查点落盘；resume 只重跑该节点并喂回外部值。"""

from src.kernel import FnBody, InMemoryStore, Node, Outcome, Plan, RunState, Scheduler


def build_plan():
    p = Plan()

    def approve(_x, ctx):
        if ctx.resume_value is None:
            return Outcome.park("approval", question="批准吗？")
        return Outcome.ok(ctx.resume_value)

    p.add(Node("approve", FnBody(approve), terminal=True))
    return p


async def test_suspend_then_resume():
    sch = Scheduler()
    run = await sch.run(build_plan())
    assert run.state == RunState.SUSPENDED
    assert run.interrupt.question == "批准吗？"

    rid = run.run_id
    run2 = await sch.resume(build_plan(), rid, {"approved": True})
    assert run2.state == RunState.COMPLETED
    assert run2.final_output == {"approved": True}


async def test_resume_with_a_fresh_scheduler_shared_store():
    """换一个全新调度器，只要接上同一个检查点存储，也能接着跑（进程重启的缩影）。"""
    store = InMemoryStore()
    sch1 = Scheduler(store=store)
    run = await sch1.run(build_plan())
    assert run.state == RunState.SUSPENDED

    sch2 = Scheduler(store=store)
    run2 = await sch2.resume(build_plan(), run.run_id, "ok")
    assert run2.state == RunState.COMPLETED
    assert run2.final_output == "ok"
