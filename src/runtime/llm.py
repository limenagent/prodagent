"""llm — scripted model for teaching/tests, plus unified message helpers.

In a real project this is where OpenAI/Anthropic adapters would live (they only
need to satisfy the kernel's LlmPort); to let every example run offline and
deterministically, ScriptedLlm answers from a preset script, one item per call.
"""

from __future__ import annotations

import os
from typing import Any

from src.kernel import LlmReply, ToolCall
from src.runtime.openai_lite import OpenAICompatibleLlm


class ScriptedLlm:
    """Return scripted answers in order; the i-th call returns the i-th item.

    A script item supports three shorthands:
    - a plain string: a text answer;
    - ToolCall / [ToolCall, ...]: a request to call tool(s);
    - LlmReply: returned as-is (the most complete form).
    """

    def __init__(self, script: list[Any], *, system_reply: str | None = None):
        self.script = list(script)
        self.system_reply = system_reply
        self.messages_seen: list[list[dict]] = []

    async def chat(self, messages, *, tools=None, system=None, on_delta=None) -> LlmReply:
        self.messages_seen.append(list(messages))
        if not self.script:
            reply = LlmReply(text=self.system_reply or "(script exhausted)")
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
        if on_delta and reply.text:  # stream once even offline, so UI behavior matches
            await on_delta(reply.text)
        return reply


def env_llm(fallback: Any):
    """Use a real OpenAI-compatible model if OPENAI_API_KEY is set, else the fallback.

    Examples and the playground all use this: zero-config offline demos, and one
    `export` switches to a real model.
    """
    if os.getenv("OPENAI_API_KEY"):
        return OpenAICompatibleLlm()
    return fallback
