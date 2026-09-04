"""memory —— 长期记忆是一种可替换策略（第 23 课）。

记忆不是和上下文混在一起的“第二份对话”，它是跨会话存在、按需检索后注入的
外部知识。这里只立一个最小可用的形态：一条统一记录 + 一组正交标签
（谁的、什么类别、多重要），而不是短期/长期/实体三座孤岛。

- remember：写入一条带标签的事实；
- recall：按当前问题检索相关记录（教学版用关键词重叠打分，生产换向量检索即可）。

ReAct 配方在 think 前调用 recall，把结果拼进 system——内核依然不知道记忆存在。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Protocol


@dataclass
class MemoryRecord:
    content: str
    tags: list[str] = field(default_factory=list)
    importance: float = 1.0
    record_id: str = ""


class Memory(Protocol):
    async def remember(self, content: str, *, tags: list[str] | None = ..., importance: float = ...) -> None: ...
    async def recall(self, query: str, *, k: int = ...) -> str: ...


def _tokens(text: str) -> set[str]:
    # 教学版：英文按词、中文按单字切分，足够演示相关性排序。
    return set(re.findall(r"[a-zA-Z]+|[\u4e00-\u9fff]", text.lower()))


class InMemoryMemory:
    def __init__(self):
        self._records: list[MemoryRecord] = []
        self._seq = 0

    async def remember(self, content: str, *, tags: list[str] | None = None,
                       importance: float = 1.0) -> MemoryRecord:
        self._seq += 1
        record = MemoryRecord(content, list(tags or []), importance, f"m{self._seq}")
        self._records.append(record)
        return record

    async def recall(self, query: str, *, k: int = 3) -> str:
        q = _tokens(query)
        scored = []
        for r in self._records:
            overlap = len(q & _tokens(r.content + " " + " ".join(r.tags)))
            if overlap:
                scored.append((overlap * r.importance, r))
        scored.sort(key=lambda x: x[0], reverse=True)
        top = [r.content for _, r in scored[:k]]
        return "\n".join(f"- {c}" for c in top)
