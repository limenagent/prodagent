"""小美 —— 真·框架 Agent，拥有完整的 L0-L3 上下文管理 + 持久记忆。

REACTIVE 模式，挂了一个她自己的只读工具 ``check_restaurant_reviews``。她"记得住"
介绍人对大牛的评价，靠的是两层机制叠加：
  1. ``MemoryHooks`` 挂的 ``MemoryManager``——预埋的介绍人评价 CONSTRAINT 从对话第
     一轮起就被无条件注入 ``[MEMORY]``，不挂分类器，全程不做现场蒸馏（见
     ``memory.py`` 顶部说明：现场蒸馏不区分"说别人"还是"说自己"，真实 LLM 下会把
     小美自曝过敏那句话蒸馏成关于她自己的记忆，这正是要避免的自我 recall 问题）。
  2. 调小的 ``ContextConfig(max_tokens=...)``——大牛把工具搜索结果原文转发过来时，
     真实撑爆窗口触发 L1 压缩；第 4 轮她自己再调用一次 ``check_restaurant_reviews``，
     这次产生的是她自己 Agent 里真实的 tool_call/tool_result 消息对，被
     ``ToolCompressStage`` 真实压缩掉中段——不再是"大牛转发的文本旁敲侧击"，压缩
     真实发生在她自己的工具往返上。
和大牛的简单版 Agent（``niu.py``）形成"真正的 prodagent 引擎 vs 简陋 demo agent"
的核心反差。

迁移到 Ensemble 后，小美用 :class:`MeiFloorMember` 适配——继承框架的
``AgentFloorMember``，复用 ``[FLOOR]`` 块的 L2 注入机制（floor transcript 走她的
压缩/记忆管线，跟 ``[MEMORY]`` 同级）。导演提示以 ``[导演提示：...]`` 追加在喂给她
的 message 末尾——跟原 orchestrator 的注入方式形似，但本质不同：原来靠 Python 侧
``for round_num`` 字符串拼接，现在走 ``FloorMember.speak()`` 的统一通道，框架对
两人一视同仁。
"""

from __future__ import annotations

import dataclasses
import os
from typing import TYPE_CHECKING

from prodagent import Agent, AgentConfig, ContextConfig, FrameworkConfig
from prodagent.core.types import ExecutionMode
from prodagent.hooks.bundles.memory import MemoryHooks
from prodagent.runtime.coordination.ensemble import AgentFloorMember
from prodagent.runtime.coordination.floor import SharedFloor

from dating_chat.fake_llm import MEI_SYSTEM_PROMPT, build_mei_fake_llm
from dating_chat.memory import MEMORY_DIR, build_memory
from dating_chat.tools import check_restaurant_reviews

if TYPE_CHECKING:
    from collections.abc import Callable

    from prodagent import MemoryManager

_CONTEXT_CONFIG = ContextConfig(max_tokens=6000)


def build_dating_chat_agent(
    *,
    memory: MemoryManager | None = None,
    framework_config: FrameworkConfig | None = None,
    run_id: str | None = None,
) -> Agent:
    """组装小美的 Agent —— REACTIVE 模式，挂 check_restaurant_reviews 工具 + MemoryHooks。"""
    if framework_config is not None:
        fw = dataclasses.replace(framework_config, context=_CONTEXT_CONFIG)
    else:
        fw = FrameworkConfig(context=_CONTEXT_CONFIG)

    if memory is not None:
        resolved_memory = memory
    else:
        memory_dir = (MEMORY_DIR / run_id) if run_id else None
        resolved_memory = build_memory(memory_dir=memory_dir)

    use_fake = os.getenv("USE_FAKE_LLM", "").lower() in ("1", "true", "yes")
    llm = build_mei_fake_llm() if use_fake else None

    return Agent(
        "mei",
        system_prompt=MEI_SYSTEM_PROMPT,
        tools=[check_restaurant_reviews],
        mode=ExecutionMode.REACTIVE,
        config=AgentConfig(
            name="mei",
            llm=llm,
            framework=fw,
            extensions=[MemoryHooks(resolved_memory)],
        ),
    )


class MeiFloorMember(AgentFloorMember):
    """小美的 FloorMember 适配器 —— 继承 AgentFloorMember，加导演提示注入。

    复用框架的 ``AgentFloorMember.speak()``：floor transcript 投影后走 ``[FLOOR]``
    L2 注入，``agent.chat()`` 跑完拿 ``AgentRun`` 封成 ``FloorTurn``。这里只覆写
    ``_build_prompt``，按小美的第几次发言（0-based）追加对应的 ``[导演提示：...]``——
    跟原 orchestrator 的 ``stage_hint()`` 语义一致，但走的是 ``FloorMember`` 的统一
    通道，不再靠 Python 侧 ``for round_num`` 字符串拼接。

    第 4 次发言（index 3）的提示需要知道大牛上一句有没有踩海鲜关键词——这里接收一个
    ``niu_last_said_getter`` 回调，由 orchestrator 提供最新的大牛发言。
    """

    def __init__(
        self,
        agent: Agent,
        *,
        session_id: str,
        stage_hints: dict[int, str],
        trap_hint_factory: Callable[[str], str],
        niu_last_said_getter: Callable[[], str],
    ) -> None:
        super().__init__(agent, session_id=session_id)
        self._stage_hints = stage_hints
        self._trap_hint_factory = trap_hint_factory
        self._niu_last_said_getter = niu_last_said_getter
        self._speak_count = 0

    def _build_prompt(self, floor: SharedFloor) -> str:
        # 基类已经拼好了"X 刚刚发言，请回应"的 prompt；这里在末尾追加导演提示。
        base = super()._build_prompt(floor)
        self._speak_count += 1
        # 小美的发言序号 1-4 对应原 orchestrator 的 round 1-4。
        mei_round = self._speak_count
        if mei_round in self._stage_hints:
            hint = self._stage_hints[mei_round]
        else:
            # 第 4 次发言（round 4）：根据大牛上一句有没有海鲜关键词决定踩雷/没踩雷。
            niu_last = self._niu_last_said_getter()
            hint = self._trap_hint_factory(niu_last)
        return f"{base}\n\n[导演提示：{hint}]"


__all__ = ["build_dating_chat_agent", "MeiFloorMember"]
