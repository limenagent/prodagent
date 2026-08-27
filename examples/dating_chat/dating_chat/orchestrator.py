"""自主对话驱动 —— 大牛与小美轮流说话，全程无人工介入。

迁移到 Ensemble 后，原来的 ``for round_num in range(...)`` 手搓循环换成
框架的 ``ensemble_stream()``：两人都是 ``FloorMember``，挂在同一个 ``SharedFloor``
上，框架负责轮次驱动、预算刹车、终止判决。叙事可靠性依然靠几个确定性机制，但现在
都走框架通道，不是 Python 侧字符串拼接：

  0. 大牛先开口打招呼、问小美最近怎么样（floor round 0），小美借着这个话头讲自己
     前两天吃海鲜过敏躺了两天——大牛因此真实"听到"过这条信息（floor transcript 里有）。
  1. 预埋记忆（``memory.seed_mei_memory``）——介绍人对**大牛**的评价从对话第一轮起
     就无条件出现在小美的 ``[MEMORY]`` 里；大牛这边则完全靠自己的历史截断机制
     "忘记"第 1 轮听到的过敏爆料——忘性是截断机制的必然结果，不是没被告知过。
  2. 大牛的历史截断（``niu.py::NiuFloorMember``）——他只读 floor 最近
     ``NIU_KEEP_MESSAGES`` 条，早期轮次根本不看，忘性体现在机制上；他把
     ``search_restaurant`` 的原始结果原文转发给小美时也不做任何压缩。
  3. 第 3 次发言（round 2-3）真实工具往返 + 原始结果转发——大牛把 ``search_restaurant``
     的原始结果原文转发给小美，真实撑爆她的 ``ContextConfig``。
  4. 第 4 次发言（round 3-4）小美想起预埋的"介绍人评价"记忆，推断大牛选餐厅大概率
     没细看详情，于是自己也调用一次 ``check_restaurant_reviews`` 工具——这次是她自己
     Agent 里真实的 tool_call/tool_result，被 ``ToolCompressStage`` 真实压缩掉中段。
  5. 导演提示——``MeiFloorMember._build_prompt`` 按小美的第几次发言追加对应的
     ``[导演提示：...]``，第 4 次还会扫描大牛上一句话有没有海鲜关键词。大牛的开场
     靠 ``NiuFloorMember.opening_hint`` 注入，其他轮次大牛只接收小美回复的原文。

跟原版的区别：floor round 编号变了（round-robin 下两人交错发言，小美的第 N 次发言
发生在 floor round N-1），但小美的发言序号（``MeiFloorMember._speak_count``）和大牛
的发言序号（``NiuFloorMember._speak_count``）各自独立计数，跟原 orchestrator 的
round_num 语义一致——叙事节拍不变。
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from prodagent import MaxRounds, TerminationPolicy
from prodagent.coordination.ensemble import (
    EnsembleSpec,
    FloorTurnEvent,
    ensemble_stream,
)
from prodagent.coordination.ensemble import PublicTextOnly
from prodagent.kernel.budget import BudgetLedger, HardBudget

from dating_chat.agent import MeiFloorMember, build_dating_chat_agent
from dating_chat.memory import MEMORY_DIR, build_memory, seed_mei_memory
from dating_chat.niu import (
    NIU_KEEP_MESSAGES,
    NIU_TRUNCATE_AFTER_ROUND,
    NiuFloorMember,
    build_niu_llm,
)
from dating_chat.tools import search_restaurant
from dating_chat.turn_signals import attach_turn_signals, pop_signals

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

TOTAL_ROUNDS = 4
SEARCH_ROUND = 3  # 大牛的第 3 次发言（round-robin 下发生在 floor round 2）

# 大牛发言 5 次（开场 + 4 轮），小美发言 4 次（4 轮）。round-robin 下大牛先，
# 顺序：大牛 R0, 小美 R0, 大牛 R1, 小美 R1, ..., 大牛 R4。小美不在 R4 发言——
# 她的第 4 次发言（揭穿）在 R3，之后大牛 R4 收尾（"啊？？你什么时候说过"）。
_NIU_TURNS = 5
_MEI_TURNS = 4


@dataclass
class DatingOrder:
    """大牛先开口，之后两人交错；大牛发完 5 次、小美发完 4 次后返回 None。

    叙事轨迹是不对称的（大牛多一次收尾），用 round-robin + 纯 max_rounds 表达不了
    "小美只发 4 次"。这里直接用发言次数计数——比加一个 MemberTurnCap 业务策略更
    直白，因为这个示例的轨迹本来就是确定性的。
    """

    niu_name: str = "大牛"
    mei_name: str = "mei"
    niu_cap: int = _NIU_TURNS
    mei_cap: int = _MEI_TURNS

    def next_speaker(self, floor):
        niu_count = sum(1 for t in floor.transcript if t.speaker == self.niu_name)
        mei_count = sum(1 for t in floor.transcript if t.speaker == self.mei_name)
        # 大牛先发言；之后每次轮到的人如果还没到 cap 就发。
        if not floor.transcript:
            return self.niu_name
        last = floor.transcript[-1].speaker
        if last == self.niu_name:
            # 刚大牛发完，轮到小美——如果她还没到 cap。
            if mei_count < self.mei_cap:
                return self.mei_name
            return None
        # 刚小美发完，轮到大牛——如果他还没到 cap。
        if niu_count < self.niu_cap:
            return self.niu_name
        return None

_SEAFOOD_KEYWORDS = ("海鲜", "虾", "蟹", "贝", "生蚝")

_ONLY_THIS = "只做这一件事，不要涉及其他话题，不要提前安排见面时间/地点等细节，控制在 1-3 句话以内。"

_NIU_OPENING_HINT = (
    "[导演提示：这是相亲后第一次主动找小美打招呼，礼貌一点，问问她最近怎么样。"
    "只问这一句，不要涉及其他话题，控制在 1-2 句话以内。]"
)

_STAGE_HINTS: dict[int, str] = {
    1: f"大牛开口问你最近怎么样，你有点不好意思地讲讲自己前两天吃海鲜过敏躺了两天、"
    f"刚缓过来的糗事，强调一句自己对海鲜真的不能沾。{_ONLY_THIS}",
    2: f"只回答自己周末喜欢干嘛，强调喜欢安静、人少的地方，不喜欢嘈杂拥挤——不要"
    f"反问对方工作忙不忙。{_ONLY_THIS}",
    3: f"说自己恢复得差不多了，提议一起吃饭见见面，让大牛帮忙查一下附近评分高的餐厅。"
    f"{_ONLY_THIS}",
}

_RECALL_MOTIVATION = (
    "你想起介绍人提前说过大牛这人大大咧咧、丢三落四，一想到他选餐厅大概率只看了评分"
    "没细看详情，你决定还是自己心里核实一下，而不是照单全收。"
)

_TRAP_HIT_HINT = (
    f"{_RECALL_MOTIVATION}先调用 check_restaurant_reviews 工具查一下大牛刚才报出的"
    "这家餐厅的详细评价——结果会显示这家提到了你过敏的海鲜类关键词，而且吵闹拥挤，"
    "踩雷了。生气/失望地指出两层意思：第一，你明明说过对海鲜过敏、也说过喜欢安静"
    "人少的地方，他都没放在心上；第二，他这上下文管理也太不靠谱了，建议他赶紧升级"
    "一下，推荐用 prodagent，还免费——用你自己的话自然地表达出这两层意思即可，不"
    "要求逐字复述。表达完就结束，不要再讨论订位、时间等后续安排。"
)
_TRAP_MISS_HINT = (
    f"{_RECALL_MOTIVATION}先调用 check_restaurant_reviews 工具查一下大牛刚才报出的"
    "这家餐厅的详细评价——结果会显示这家很安全，没有踩雷，正常愉快地回应一句就好，"
    "不要无缘无故生气，也不要主动往后延伸新的安排（比如订位、时间）。"
)


def stage_hint(round_num: int, niu_last_said: str) -> str:
    """保留原 API —— 供 MeiFloorMember 的 trap_hint_factory 调用。"""
    if round_num < TOTAL_ROUNDS:
        return _STAGE_HINTS[round_num]
    hit = any(keyword in niu_last_said for keyword in _SEAFOOD_KEYWORDS)
    return _TRAP_HIT_HINT if hit else _TRAP_MISS_HINT


@dataclass(frozen=True, slots=True)
class Line:
    speaker: str
    text: str
    round: int
    memory_hits: int = 0
    tool_calls: tuple[str, ...] = ()
    memory_previews: tuple[str, ...] = ()
    memory_written: tuple[str, ...] = ()
    compression: str = ""
    history_summary: str = ""
    tool_compress_sample: str = ""
    niu_note: str = ""
    floor_snapshot: dict[str, Any] = field(default_factory=dict)


async def run_conversation(*, session_id: str = "dating-chat") -> AsyncIterator[Line]:
    """跑完整对话，逐行 yield 出来给 CLI 打印或 web 层推送。

    跟原版的区别：不再用 ``for round_num`` 手搓循环，而是组装 ``EnsembleSpec`` 交给
    ``ensemble_stream()``。框架按 round-robin 驱动两人发言，每轮检查共享 BudgetLedger +
    TerminationPolicy。两人都是 ``FloorMember``——大牛是 ``NiuFloorMember``（手搓
    messages + 截断），小美是 ``MeiFloorMember``（真 Agent + [FLOOR] L2 注入）。
    """
    memory = build_memory(memory_dir=MEMORY_DIR / session_id, clean=True)
    await seed_mei_memory(memory)

    mei_agent = build_dating_chat_agent(memory=memory, run_id=session_id)
    signals = attach_turn_signals(mei_agent)
    niu_llm = build_niu_llm()

    # 大牛的最新发言文本 —— 供小美第 4 次发言的 trap_hint_factory 扫海鲜关键词。
    # 用一个单元素 list 当可变闭包变量（闭包不能直接赋值外层变量）。
    niu_last_said_box: list[str] = [""]

    niu_member = NiuFloorMember(
        niu_llm,
        opening_hint=_NIU_OPENING_HINT,
        search_tools=[search_restaurant],
        search_round=SEARCH_ROUND,
    )
    mei_member = MeiFloorMember(
        mei_agent,
        session_id=session_id,
        stage_hints=_STAGE_HINTS,
        trap_hint_factory=lambda niu_last: stage_hint(TOTAL_ROUNDS, niu_last),
        niu_last_said_getter=lambda: niu_last_said_box[0],
    )

    # max_rounds=10 作为硬底线（DatingOrder 会在 9 turns 后返回 None 先停）。
    # floor round 编号：大牛 R0, 小美 R0, 大牛 R1, 小美 R1, ..., 大牛 R4 = 9 turns。
    spec = EnsembleSpec(
        members=[niu_member, mei_member],
        topic="相亲第一次聊天",
        order=DatingOrder(),
        projection=PublicTextOnly(),
        termination=TerminationPolicy(hard_cap=MaxRounds(max_rounds=10)),
        budget=BudgetLedger(max=HardBudget(
            max_turns=20, max_seconds=1800.0, max_tokens=500_000, max_cost_usd=2.0,
        )),
        session_id=session_id,
    )

    async for event in ensemble_stream(spec):
        if not isinstance(event, FloorTurnEvent):
            continue
        turn = event.turn
        if turn.speaker == "mei":
            # MeiFloorMember.speak() 调 agent.chat() 后暴露 last_run_id，用它 pop 信号。
            run_id = mei_member.last_run_id
            sig = pop_signals(signals, run_id)
            mei_tool_calls = tuple(call.name for call in turn.tool_calls)
            yield Line(
                "小美",
                turn.text,
                turn.round,
                memory_hits=sig.memory_hits,
                tool_calls=mei_tool_calls,
                memory_previews=tuple(sig.memory_previews),
                compression=sig.compression,
                history_summary=sig.history_summary,
                tool_compress_sample=sig.tool_compress_sample,
                floor_snapshot=event.floor_snapshot,
            )
        elif turn.speaker == "大牛":
            niu_last_said_box[0] = turn.text
            tool_calls = tuple(call.name for call in turn.tool_calls)
            niu_note = ""
            niu_speak_count = niu_member._speak_count  # noqa: SLF001 — 同包内部访问
            if niu_speak_count == SEARCH_ROUND and niu_member.last_tool_result_full:
                niu_note = (
                    "大牛没有任何上下文管理：工具返回的完整原始结果被直接塞进对话，"
                    "不做压缩也不做摘要"
                )
            elif niu_speak_count > NIU_TRUNCATE_AFTER_ROUND:
                niu_note = (
                    f"大牛的历史已被机械截断，只保留最近 {NIU_KEEP_MESSAGES} 条消息——"
                    "第 1 轮的过敏爆料已经从他的上下文里彻底消失"
                )
            yield Line(
                "大牛",
                turn.text,
                turn.round,
                tool_calls=tool_calls,
                niu_note=niu_note,
                floor_snapshot=event.floor_snapshot,
            )


async def main() -> None:
    async for line in run_conversation():
        print(f"【{line.speaker}】{line.text}")


if __name__ == "__main__":
    asyncio.run(main())


__all__ = ["TOTAL_ROUNDS", "Line", "run_conversation"]
