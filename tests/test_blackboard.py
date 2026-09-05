"""黑板模式：异构专家并行写共享通道，主持人 join=all 汇聚，多轮趋同。"""

from src.kernel import (
    Bus,
    FnBody,
    InMemoryEventLog,
    InMemoryStore,
    Outcome,
    Scheduler,
)
from src.runtime.multiagent import build_blackboard

NAMES = ["alice", "bob", "carol"]


def make_expert(name):
    async def body(_, ctx):
        r = ctx.shared["round"]
        # 教学确定性：第一轮 carol 反对，第二轮被说服，用来验证“多轮趋同”。
        vote = "agree" if (name != "carol" or r >= 1) else "object"
        return Outcome.ok(name, board=[f"{name}:{vote}"])

    return FnBody(body)


async def moderator(_, ctx):
    r = ctx.shared["round"]
    last_votes = [b.split(":", 1)[1] for b in ctx.shared["board"][-len(NAMES) :]]
    if all(v == "agree" for v in last_votes):
        return Outcome.goto("final", verdict="consensus")
    return Outcome.goto("fanout", round=r + 1)  # 未达成：回边再来一轮


async def test_blackboard_converges_after_two_rounds():
    plan = build_blackboard(
        [(n, make_expert(n)) for n in NAMES],
        FnBody(moderator),
    )
    sch = Scheduler(bus=Bus(), eventlog=InMemoryEventLog(), store=InMemoryStore())
    run = await sch.run(plan, task="评审")
    assert run.state.name == "COMPLETED"
    assert run.shared["verdict"] == "consensus"
    assert run.shared["round"] == 1  # 确实转了两圈，不是第一轮空过
    assert len(run.shared["board"]) == 6  # 三个专家 × 两轮，意见都留在黑板上


async def test_blackboard_single_round_when_all_agree():
    async def all_agree(_, ctx):
        return Outcome.goto("final", verdict="consensus")

    plan = build_blackboard([(n, make_expert(n)) for n in ["alice", "bob"]], FnBody(all_agree))
    sch = Scheduler(bus=Bus(), eventlog=InMemoryEventLog(), store=InMemoryStore())
    run = await sch.run(plan, task="评审")
    assert run.shared["verdict"] == "consensus"
    assert run.shared["round"] == 0
