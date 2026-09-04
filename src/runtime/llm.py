"""llm —— 教学/测试用的脚本化模型，以及统一消息工具。

真实项目里这里会放 OpenAI/Anthropic 的适配（满足 kernel 的 LlmPort 即可）；
为了让所有 example 离线、确定性地跑，我们用 ScriptedLlm 按预设脚本依次应答。
"""

from __future__ import annotations

import os
from typing import Any

from src.kernel import LlmReply, ToolCall
from src.runtime.openai_lite import OpenAICompatibleLlm


class ScriptedLlm:
    """按给定脚本依次返回应答；第 i 次调用返回脚本第 i 项。

    脚本项支持三种简写：
    - 字符串：纯文本应答；
    - ToolCall / [ToolCall, ...]：请求调用工具；
    - LlmReply：原样返回（最完整）。
    """

    def __init__(self, script: list[Any], *, system_reply: str | None = None):
        self.script = list(script)
        self.system_reply = system_reply
        self.messages_seen: list[list[dict]] = []

    async def chat(self, messages, *, tools=None, system=None, on_delta=None) -> LlmReply:
        self.messages_seen.append(list(messages))
        if not self.script:
            reply = LlmReply(text=self.system_reply or "(脚本已耗尽)")
        else:
            item = self.script.pop(0)
            if isinstance(item, LlmReply):
                reply = item
            elif isinstance(item, ToolCall):
                reply = LlmReply(tool_calls=[item], tokens=6)
            elif isinstance(item, list) and all(isinstance(x, ToolCall) for x in item):
                reply = LlmReply(tool_calls=item, tokens=6)
            else:
                reply = LlmReply(text=str(item), tokens=6)
        if on_delta and reply.text:  # 离线也走一次吐字，UI 行为一致
            await on_delta(reply.text)
        return reply


def env_llm(fallback: Any):
    """配了 OPENAI_API_KEY 就用真实的 OpenAI 兼容模型，否则用传入的离线模型。

    示例与 playground 统一用它：零配置可离线演示，export 一下就切真实模型。
    """
    if os.getenv("OPENAI_API_KEY"):
        return OpenAICompatibleLlm()
    return fallback


def user_message(text: str) -> dict:
    return {"role": "user", "content": text}


def tool_message(name: str, content: Any) -> dict:
    return {"role": "tool", "name": name, "content": content}
