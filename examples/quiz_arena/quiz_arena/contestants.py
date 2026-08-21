"""选手 —— 真·框架 Agent，用手写 ``BlackboardMember`` 适配（不是
:class:`~prodagent.runtime.coordination.blackboard.AgentBlackboardMember`）。

框架自带的 ``AgentBlackboardMember`` 把结果写成一段纯文本，够用但丢失了
"谁答的、答的哪道题"这类结构化信息——它自己的 docstring 也明说了这是留给
简单场景的参考实现，"有结构化输出需求的调用方应该直接实现 BlackboardMember"。
选手抢答刚好是这个场景：主持人评分需要拿到 ``{contestant, question_id, text}``
整个结构，所以这里直接实现协议，只在"调 Agent、拿文本"这一步复用
``Agent.chat()``。

``try_contribute`` 入口第一行自增 ``compute_count``——这是本 demo 自证"抢答
输家从不计算"的关键探针：跑完全场后 ``show.py`` 会断言
``sum(compute_count for 每位选手) == 已问出的题数``，也就是每道题必然
只有一位选手真正调用过 LLM。
"""

from __future__ import annotations

import os
import re

from prodagent import (
    Agent,
    AgentConfig,
    Board,
    BoardWrite,
    FakeLLMAdapter,
    FrameworkConfig,
    Trigger,
)
from prodagent.core.types import ExecutionMode, LLMResponse, StopReason

_HINT_PATTERN = re.compile(r"\[提示：正确答案是\s*(.+?)\]")


class _HintEchoLLM(FakeLLMAdapter):
    """把私密提示当答案回声的离线适配器。

    ``REACTIVE`` 模式每轮都会在 messages 末尾追加一条 ``[STATE]`` 的
    user 消息（框架的 turn/state 记账，与本 demo 无关）。基类
    ``FakeLLMAdapter`` 的兜底逻辑只回声"最后一条 user 消息"，会被这条
    ``[STATE]`` 消息挡住，永远看不到真正带 ``[提示：...]`` 的那条。这里
    改成倒序扫描全部消息、找第一条带私密提示的，绕开这个顺序依赖，不用
    改动框架的 REACTIVE 循环。
    """

    async def complete(self, messages, **kwargs):  # type: ignore[override]
        for message in reversed(messages):
            content = message.get("content") or ""
            if _HINT_PATTERN.search(content):
                self._call_count += 1
                return LLMResponse(
                    content=content,
                    stop_reason=StopReason.END_TURN,
                    input_tokens=50,
                    output_tokens=10,
                )
        return await super().complete(messages, **kwargs)


def extract_answer(raw_text: str) -> str:
    """从选手的原始回复里剥出真正的答案文本。

    离线 FakeLLM 模式下 ``FakeLLMAdapter`` 没有真实推理能力，只会把整段
    prompt 原样回声——真正驱动"答对"的是 prompt 里挂的 ``[提示：...]``
    私密提示（跟 ``dating_chat`` 的 ``[导演提示：...]`` 是同一套约定：只有
    "演员"能看到、绝不代表真实推理）。接真实 LLM 时 prompt 里不会有这段
    提示，此函数原样返回整段回复。
    """
    match = _HINT_PATTERN.search(raw_text)
    return match.group(1).strip() if match else raw_text.strip()


def _use_fake_llm() -> bool:
    return os.getenv("USE_FAKE_LLM", "").lower() in ("1", "true", "yes")


def build_contestant_agent(name: str, *, specialty: str) -> Agent:
    system_prompt = (
        f"你是抢答竞赛选手{name}，擅长{specialty}类题目。"
        "听到题目后直接说出答案，不要解释、不要复述题目。"
        "如果完全不知道，就诚实地说“不知道”。"
    )
    llm = _HintEchoLLM() if _use_fake_llm() else None
    return Agent(
        name,
        system_prompt=system_prompt,
        mode=ExecutionMode.REACTIVE,
        config=AgentConfig(
            name=name,
            llm=llm,
            framework=FrameworkConfig.default(),
        ),
    )


class ContestantMember:
    """一位选手的 ``BlackboardMember`` 适配——只在抢到锁时才会被主循环调用。"""

    def __init__(self, agent: Agent, *, session_id: str) -> None:
        self._agent = agent
        self._session_id = session_id
        self.name = agent.name
        self.compute_count = 0

    async def try_contribute(self, board: Board, *, trigger: Trigger) -> BoardWrite | None:
        # 因为是 buzz_in 抢答，能走到这个方法本身就证明这一位抢到了锁——
        # 落选的选手连 try_contribute 都不会被调用一次，见 blackboard.py
        # 的 _dispatch_buzz_in()。compute_count 只在真正调用 LLM 前才自增，
        # 精确统计"这一整场我真的算过几次"。
        state = (board.read(["state"]) or {}).get("state") or {}
        question = state.get("current_question")
        if question is None:
            return None

        prompt = f"抢答题（{question['category']}）：{question['text']}\n请直接给出你的答案。"
        if _use_fake_llm():
            # 离线演示专用的私密提示——见 extract_answer() 顶部说明。
            prompt += f"\n[提示：正确答案是 {question['answer']}]"

        self.compute_count += 1
        run = await self._agent.chat(prompt, session_id=self._session_id)
        raw = (run.final_output or "").strip()
        if not raw:
            return None

        return BoardWrite(
            key="answer",
            value={
                "contestant": self.name,
                "question_id": question["id"],
                "text": extract_answer(raw),
            },
            author=self.name,
            cost_usd=float(getattr(run, "cost_usd", 0.0) or 0.0),
        )


__all__ = ["ContestantMember", "build_contestant_agent", "extract_answer"]
