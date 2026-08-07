"""供 playground server 调用的入口 —— 把 ``orchestrator.run_conversation()`` 的输出
推进 ``asyncio.Queue``，供 SSE 推给气泡页面。

不复用 ``playground.server`` 的 ``RunContext``/``WebPushHooks``——那一套是为"单 Agent +
通用 hook 事件日志流"设计的，这里只有"两个话者轮流吐一句话"的简单气泡流，直接用一个
干净的队列协议：``{"type": "message", "speaker": ..., "text": ..., "round": n,
"memory_hits": n, "memory_previews": [...], "compression": "...",
"history_summary": "...", "tool_compress_sample": "...", "tool_calls": [...],
"niu_note": "..."}``，结束时推 ``{"type": "done"}``。``tool_compress_sample`` 是
``TOOL_COMPRESS`` 压缩后仍保留在尾部的原文片段；``niu_note`` 仅在大牛这边非空，标出
他这一轮暴露出的"没有上下文管理"具体问题。
"""

from __future__ import annotations

import asyncio
from typing import Any

from dating_chat.orchestrator import run_conversation

_BUBBLE_DELAY_S = 0.8


async def run_autonomous_chat(queue: asyncio.Queue[dict[str, Any]], *, session_id: str) -> None:
    """跑完整一场自主对话，逐条把气泡事件推进 ``queue``，结束后推 ``done``。"""
    try:
        async for line in run_conversation(session_id=session_id):
            await queue.put(
                {
                    "type": "message",
                    "speaker": line.speaker,
                    "text": line.text,
                    "round": line.round,
                    "memory_hits": line.memory_hits,
                    "memory_previews": list(line.memory_previews),
                    "compression": line.compression,
                    "history_summary": line.history_summary,
                    "tool_compress_sample": line.tool_compress_sample,
                    "tool_calls": list(line.tool_calls),
                    "niu_note": line.niu_note,
                }
            )
            await asyncio.sleep(_BUBBLE_DELAY_S)
    except Exception as exc:  # noqa: BLE001 — surface to the UI instead of dying silently
        await queue.put({"type": "failed", "error": f"{type(exc).__name__}: {exc}"})
    finally:
        await queue.put({"type": "done"})


__all__ = ["run_autonomous_chat"]
