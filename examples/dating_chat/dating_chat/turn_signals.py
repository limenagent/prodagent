"""挂在小美 Agent 上的轻量 hook 监听器 —— 采集每轮的记忆 + 压缩信号。

``Agent.chat()`` 的返回值不带 hook 事件列表，要拿到"这一轮 [MEMORY] 召回了几条、
召回的是什么内容""这一轮历史有没有被压缩"这类信号，必须直接挂 ``HookRegistry``
监听器：

- ``HookEvent.MEMORY_RECALL``（``context/manager.py``）—— 每轮 `[MEMORY]` 块组装完
  就会触发，带 ``hits``（命中数）和 ``previews``（每条命中内容掐头去尾到 80 字符），
  证明气泡里显示的不是编的台词，是 ``RuleChannel`` 真实召回的原文。小美的
  ``MemoryManager`` 全程不挂分类器（见 ``memory.py``），召回的永远只有预埋的那条
  介绍人评价 CONSTRAINT，不会有现场蒸馏出来的内容混进来。
- ``HookEvent.CONTEXT_BUILD``（``context/manager.py``）—— 每轮都会触发，带
  ``compression``（``CompressionLevel`` 名字，``NONE`` 表示没触发）；如果触发了
  摘要级压缩，``messages`` 里会混入一条 ``[HISTORY SUMMARY]``/``[TOPIC SUMMARY]``
  前缀的消息，从里面截一段展示，证明压缩留下的是真实摘要而不是空气。同样地，
  如果触发的是 ``TOOL_COMPRESS``，``messages`` 里会有一条 ``role == "tool"`` 且
  ``content`` 含 ``"chars omitted"`` 的消息——``compress_tool_result()``
  （``context/compression/formatting.py``）保留了该内容的 head+tail，我们截取
  尾部一段存进 ``tool_compress_sample``，这是压缩后仍保留下来的真实原文（比如
  ``check_restaurant_reviews`` 结果里的 ``noise_level`` 字段天然落在尾部），
  用来证明"压缩不是把核心信号一起丢掉"。

编排层对同一个 ``session_id`` 反复调用 ``chat()``，每次产生不同的 ``run_id``，所以用
``run_id`` 做 key 把两类事件数据汇总起来，``chat()`` 返回后取出即弹出，避免无限增长。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from prodagent.cognition.context.budget import CompressionLevel
from prodagent.hooks.events import HookEvent

if TYPE_CHECKING:
    from prodagent.runtime.agent import Agent

_SUMMARY_PREFIXES = ("[HISTORY SUMMARY]", "[TOPIC SUMMARY]")
_SUMMARY_PREVIEW_LEN = 80
_TOOL_COMPRESS_MARKER = "chars omitted"
_TOOL_COMPRESS_SAMPLE_LEN = 120


@dataclass
class TurnSignals:
    memory_hits: int = 0
    memory_previews: list[str] = field(default_factory=list)
    compression: str = ""
    history_summary: str = ""
    tool_compress_sample: str = ""


def attach_turn_signals(agent: Agent) -> dict[str, TurnSignals]:
    hooks = agent.attach_default_hooks()
    assert hooks is not None
    store: dict[str, TurnSignals] = {}

    def _on_memory_recall(
        *, run_id: str = "", hits: int = 0, previews: list[str] | None = None, **_: Any
    ) -> None:
        signals = store.setdefault(run_id, TurnSignals())
        real_previews = [p for p in (previews or []) if not p.startswith("[FLOOR]")]
        signals.memory_hits = len(real_previews)
        signals.memory_previews = real_previews

    def _on_context_build(
        *, run_id: str = "", compression: str = "", messages: list[Any] | None = None, **_: Any
    ) -> None:
        signals = store.setdefault(run_id, TurnSignals())
        compressed_tool_result_seen = False
        for msg in messages or []:
            content = msg.get("content") if isinstance(msg, dict) else None
            if not isinstance(content, str):
                continue
            if content.startswith(_SUMMARY_PREFIXES):
                signals.history_summary = content[:_SUMMARY_PREVIEW_LEN]
            elif msg.get("role") == "tool" and _TOOL_COMPRESS_MARKER in content:
                signals.tool_compress_sample = content[-_TOOL_COMPRESS_SAMPLE_LEN:]
                compressed_tool_result_seen = True
        if compression == CompressionLevel.TOOL_COMPRESS.name and not compressed_tool_result_seen:
            signals.compression = ""
        else:
            signals.compression = compression

    hooks.register_event(HookEvent.MEMORY_RECALL, _on_memory_recall)
    hooks.register_event(HookEvent.CONTEXT_BUILD, _on_context_build)
    return store


def pop_signals(store: dict[str, TurnSignals], run_id: str) -> TurnSignals:
    return store.pop(run_id, TurnSignals())


__all__ = ["TurnSignals", "attach_turn_signals", "pop_signals"]
