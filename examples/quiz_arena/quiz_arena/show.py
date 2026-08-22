"""quiz_arena 主编排 —— 后台审题（WorkQueue）接正式抢答（Blackboard）。

两段流程共用一个题库，中间用 WorkQueue 跑出的"审核通过"结果集当作
Blackboard 阶段的输入，直观演示两个协作原语：

1. **WorkQueue**（后台审题）—— 审核员是 pull 模型的 ``Worker``：谁空闲谁去
   领下一道题审，不是被动等派发。演示租约超时重新入队（审核员"失联"）和
   重试到上限被扔进死信（题目本身有问题，永远不会浪费第二次尝试）。
2. **Blackboard**（正式抢答）—— ``kickoff`` 是 keys=[] 的常驻触发器，主持人
   借它出题、判分；``buzz_in`` 是本轮真正的主角：多名选手同时符合触发条件，
   但"先抢锁再算"——没抢到锁的人连 ``try_contribute`` 都不会被调用一次。
   跑完全场后本脚本会用 ``ContestantMember.compute_count`` 断言这一点。
"""

from __future__ import annotations

import asyncio

from prodagent import BudgetLedger, MaxRounds, TerminationPolicy, Trigger
from prodagent.coordination.blackboard import (
    BlackboardCompletedEvent,
    BlackboardSpec,
    BoardWriteEvent,
    blackboard_stream,
)
from prodagent.coordination.work_queue import (
    ItemClaimedEvent,
    ItemCompletedEvent,
    ItemDeadLetteredEvent,
    ItemRequeuedEvent,
    QueueDrainedEvent,
    WorkQueueSpec,
    work_queue_stream,
)
from prodagent.core.budget import HardBudget

from quiz_arena.contestants import ContestantMember, build_contestant_agent
from quiz_arena.host import HostMember
from quiz_arena.questions import QUESTION_BANK, build_work_items
from quiz_arena.review import FlakyReviewer, QuickReviewer

CONTESTANTS = [
    ("小明", "xiaoming", "地理和历史"),
    ("小红", "xiaohong", "文学"),
    ("小刚", "xiaogang", "科学"),
]


async def _run_backstage_review() -> dict[str, dict]:
    """WorkQueue 阶段：审完的题目 id -> payload。"""
    print("\n=== 后台审题（WorkQueue）===")
    workers = {
        "quick_reviewer": QuickReviewer("quick_reviewer"),
        "flaky_reviewer": FlakyReviewer("flaky_reviewer", hang_on="q2"),
    }
    spec = WorkQueueSpec(
        workers=workers,
        items=build_work_items(),
        lease_seconds=0.02,
        termination=TerminationPolicy(hard_cap=MaxRounds(max_rounds=50)),
    )

    validated: dict[str, dict] = {}
    by_id = {q["id"]: q for q in QUESTION_BANK}

    async for event in work_queue_stream(spec):
        if isinstance(event, ItemClaimedEvent):
            print(f"  [认领] {event.worker} 领走了 {event.item_id}")
        elif isinstance(event, ItemCompletedEvent):
            validated[event.item_id] = by_id[event.item_id]
            print(f"  [通过] {event.item_id} 审核通过 —— {event.worker}")
        elif isinstance(event, ItemRequeuedEvent):
            print(f"  [重排] {event.item_id} 因“{event.reason}”被收回重新入队")
        elif isinstance(event, ItemDeadLetteredEvent):
            print(f"  [淘汰] {event.item_id} 重试 {event.attempts} 次仍失败，进死信：{event.error}")
        elif isinstance(event, QueueDrainedEvent):
            print(f"  [完成] 审题结束 —— {event.reason.reason}: {event.reason.detail}")

    return validated


async def _run_live_quiz(validated_questions: list[dict]) -> None:
    print("\n=== 正式抢答（Blackboard）===")
    session_id = "quiz-arena-live"
    members: dict[str, object] = {"host": HostMember(validated_questions)}
    contestant_members: list[ContestantMember] = []
    for name, slug, specialty in CONTESTANTS:
        agent = build_contestant_agent(name, specialty=specialty)
        cm = ContestantMember(agent, session_id=f"{session_id}-{slug}")
        members[name] = cm
        contestant_members.append(cm)

    spec = BlackboardSpec(
        experts=members,  # type: ignore[arg-type]
        triggers={
            "kickoff": Trigger(name="kickoff", keys=[], experts=["host"], mode="event"),
            "buzz_in": Trigger(
                name="buzz_in",
                keys=["state"],
                experts=[name for name, _, _ in CONTESTANTS],
                mode="buzz_in",
            ),
        },
        termination=TerminationPolicy(hard_cap=MaxRounds(max_rounds=100)),
        budget=BudgetLedger(max=HardBudget(max_turns=200, max_seconds=60, max_cost_usd=10.0)),
        terminal_check=lambda board: bool(
            ((board.read(["state"]) or {}).get("state") or {}).get("finished")
        ),
    )

    answered = 0
    async for event in blackboard_stream(spec):
        if isinstance(event, BoardWriteEvent):
            if event.trigger_name == "kickoff":
                question = event.write.value.get("current_question")
                if question is not None:
                    print(f"  [出题] 🎙️ 主持人：下一题（{question['category']}）—— {question['text']}")
            elif event.trigger_name == "buzz_in":
                answered += 1
                v = event.write.value
                print(f"  [抢答] 🔔 {v['contestant']} 抢到并作答：{v['text']!r}")
        elif isinstance(event, BlackboardCompletedEvent):
            state = (event.board_snapshot.get("slots") or {}).get("state", {}).get("value") or {}
            print(f"\n  [结束] {event.reason.reason}: {event.reason.detail}")
            print(f"  最终比分：{state.get('scores')}")
            for line in state.get("log", []):
                print(f"    - {line}")

    total_computes = sum(cm.compute_count for cm in contestant_members)
    assert total_computes == answered, (
        f"抢答语义被破坏：{answered} 道题被回答，但选手一共真正计算了 {total_computes} 次"
        "（应该严格相等——每道题有且只有一位选手的 LLM 被调用过）"
    )
    print(
        f"\n[验证] {answered} 道题，选手合计真正计算 {total_computes} 次 —— "
        "抢答语义成立：每道题有且只有一位选手计算过，其余人从未开始。"
    )
    for cm in contestant_members:
        print(f"  {cm.name} 本场真正计算了 {cm.compute_count} 次")


async def run_quiz_arena() -> None:
    validated = await _run_backstage_review()
    print(f"\n审题结果：{len(validated)}/{len(QUESTION_BANK)} 道题通过，进入正式抢答。")
    ordered = [q for q in QUESTION_BANK if q["id"] in validated]
    await _run_live_quiz(ordered)


def main() -> None:
    asyncio.run(run_quiz_arena())


if __name__ == "__main__":
    main()

__all__ = ["run_quiz_arena", "main"]
