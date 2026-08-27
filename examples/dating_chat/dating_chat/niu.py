"""大牛 —— 屌丝开发的简单 append agent。

大牛会老老实实累积 ``messages``，问题在于攒到一定轮数后，会被不假思索地直接
切片截断。他完全绕开 prodagent 框架：没有 ``Agent``，没有 ``ContextManager``，
没有压缩，没有记忆——只有一个手搓的 ``messages`` 列表和 ``LLMClient.complete()``
调用。查餐厅那轮他用一次最朴素的工具往返（不解析结果，只是原样转发），这一步
恰恰是把小美一侧真实压缩触发起来的手段。

迁移到 Ensemble 后，大牛被包成 :class:`NiuFloorMember` —— 一个只满足
``FloorMember`` 协议（``name`` + ``async speak()``）的薄壳，依然不给他任何
prodagent 能力：没有 ``Agent``、没有 ``ContextManager``、没有 ``MemoryManager``、
没有压缩。他只读 floor 里最近几轮（按自己的截断阈值），调一次 ``niu_reply``，
把结果封成 ``FloorTurn`` 交还。框架对两人一视同仁——反差不靠"大牛在框架之外"
制造，而靠"同一 floor、同一待遇下，实现层决定你记不记得住"。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from prodagent import use_fake_llm
from prodagent.coordination.ensemble import FloorTurn, SharedFloor
from prodagent.kernel.types import MessageList, StopReason

from dating_chat.fake_llm import NIU_SYSTEM_PROMPT, build_niu_fake_llm

if TYPE_CHECKING:
    from prodagent import LLMClient
    from prodagent.ports.tool import Tool

# 从第 4 轮起，处理新输入前先把历史砍到只剩最近 2 轮问答。
NIU_TRUNCATE_AFTER_ROUND = 3
NIU_KEEP_MESSAGES = 4


async def _noop_chunk(_: str) -> None:
    return None


@dataclass
class TurnLog:
    """一轮里发生的事——供 orchestrator 决定气泡文本 vs 转发给小美的完整文本。"""

    said: str
    tool_calls: list[str] = field(default_factory=list)
    tool_result_full: str = ""


async def niu_reply(
    llm: LLMClient,
    messages: MessageList,
    incoming: str,
    round_num: int,
    *,
    tools: list[Tool] | None = None,
) -> TurnLog:
    """大牛的一轮：累积历史，但超过阈值就地砍掉早期消息。

    传入 ``tools`` 且模型返回工具调用时，执行工具、把结果原文（不截断）追加进
    ``messages``，再请求一次最终文本——大牛不解析、不总结搜索结果，只是机械转发。
    """
    if round_num > NIU_TRUNCATE_AFTER_ROUND:
        del messages[:-NIU_KEEP_MESSAGES]

    messages.append({"role": "user", "content": incoming})
    schemas = [t.schema for t in tools] if tools else []
    tool_by_name = {t.name: t for t in tools} if tools else {}

    response = await llm.complete(
        messages, system=NIU_SYSTEM_PROMPT, tools=schemas, on_chunk=_noop_chunk
    )
    if response.stop_reason != StopReason.TOOL_USE:
        messages.append({"role": "assistant", "content": response.content})
        return TurnLog(said=response.content)

    log = TurnLog(said=response.content)
    messages.append({"role": "assistant", "content": response.content})
    for call in response.tool_calls:
        log.tool_calls.append(f"{call.name}({call.params})")
        tool = tool_by_name.get(call.name)
        if tool is None:
            result_text = f"[error] unknown tool {call.name}"
        else:
            result = await tool(**call.params)
            result_text = str(result.value if result.value is not None else result.error)
        log.tool_result_full = result_text
        messages.append({"role": "user", "content": f"[{call.name} 返回] {result_text}"})

    final = await llm.complete(
        messages, system=NIU_SYSTEM_PROMPT, tools=schemas, on_chunk=_noop_chunk
    )
    messages.append({"role": "assistant", "content": final.content})
    log.said = final.content
    return log


def build_niu_llm() -> LLMClient:
    """大牛完全绕开框架，自己决定 fake/真实。"""
    use_fake = use_fake_llm()
    if use_fake:
        return build_niu_fake_llm()
    from prodagent.llm.factory import create_llm_client

    return create_llm_client()


class NiuFloorMember:
    """大牛的 FloorMember 适配器 —— 不给任何 prodagent 能力，只读最近几轮。

    满足 ``FloorMember`` 协议（``name`` + ``async speak(floor, *, round_num)``），
    内部依然是手搓 ``messages`` 列表 + ``niu_reply`` + 机械截断。floor 投影后的
    transcript 喂给他时，他只看最近 ``NIU_KEEP_MESSAGES`` 条 —— 第 1 轮小美说的
    过敏爆料，到了第 4 轮早就被他的截断丢掉，所以他会挑海鲜自助。这不是没被告知
    过，是他的实现层决定的"忘性"。

    ``opening_hint`` 是第 0 轮开场时喂给大牛的导演提示（让他主动打招呼），跟原
    orchestrator 的 ``_NIU_OPENING_HINT`` 一致；其他轮次大牛只接收小美回复的
    ``final_output``，看不到任何导演提示。
    """

    def __init__(
        self,
        llm: LLMClient,
        *,
        opening_hint: str = "",
        search_tools: list[Tool] | None = None,
        search_round: int = 3,
    ) -> None:
        self.name = "大牛"
        self._llm = llm
        self._messages: MessageList = []
        self._opening_hint = opening_hint
        self._search_tools = search_tools or []
        self._search_round = search_round
        # 大牛的"第几次发言"，0-based —— 用来对齐原 orchestrator 的 round_num 语义
        # （原脚本里 round_num 1-4 是正式轮次，0 是开场）。floor 的 round_num 不直接
        # 用，因为 round-robin 下大牛和小美的 round 编号会交错。
        self._speak_count = 0
        # 上一轮的 tool_result_full —— 供 orchestrator/web 层决定是否转发大 payload。
        self.last_tool_result_full: str = ""

    async def speak(self, floor: SharedFloor, *, round_num: int) -> FloorTurn:
        # 把 floor 投影成大牛能看到的最近几轮，拼成 incoming 文本。
        # 大牛不读全文 transcript，只读最近 NIU_KEEP_MESSAGES 条（他自己的截断语义
        # 体现在这里：早期轮次他根本不看，不是看了又忘）。
        recent = floor.recent_turns(limit=NIU_KEEP_MESSAGES)
        if not recent and self._opening_hint:
            # 第 0 轮开场：floor 还空着，用导演提示启动。
            incoming = self._opening_hint
            niu_round = 0
        else:
            # 拼最近几轮的发言作为 incoming。小美的话原文转发；如果上一轮大牛自己
            # 调了 search_restaurant，把原始结果也塞进 incoming（撑爆小美的 context）。
            parts: list[str] = []
            for turn in recent:
                if turn.speaker == self.name:
                    # 大牛不看自己说过的话再回一遍——但 niu_reply 内部 messages 已经
                    # 记着了，这里跳过即可。
                    continue
                parts.append(turn.text)
            incoming = "\n\n".join(parts) if parts else self._opening_hint
            # 大牛的发言序号：第 1 次正式发言（开场之后）算 round 1。
            self._speak_count += 1
            niu_round = self._speak_count

        # 第 search_round 轮给大牛挂上 search_restaurant 工具。
        tools = self._search_tools if niu_round == self._search_round else None

        log = await niu_reply(self._llm, self._messages, incoming, niu_round, tools=tools)
        self.last_tool_result_full = log.tool_result_full

        tool_call_names = [c.split("(", 1)[0] for c in log.tool_calls]
        from prodagent.kernel.types import ToolCall

        tool_calls = [ToolCall(name=n, params={}) for n in tool_call_names]

        return FloorTurn(
            speaker=self.name,
            round=round_num,
            text=log.said,
            tool_calls=tool_calls,
        )


__all__ = [
    "NIU_KEEP_MESSAGES",
    "NIU_TRUNCATE_AFTER_ROUND",
    "NiuFloorMember",
    "build_niu_llm",
]
