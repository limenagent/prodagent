"""memory — long-term memory as a replaceable strategy.

Memory is not a "second conversation" mixed into the context; it is external
knowledge that lives across sessions and is retrieved on demand before being
injected. Here we keep only a minimal usable shape: one unified record plus a
set of orthogonal tags (whose, what category, how important), rather than three
isolated silos of short-term / long-term / entity memory.

- remember: write a tagged fact;
- recall: retrieve records relevant to the current question (the teaching build
  scores by keyword overlap; in production swap in vector retrieval).

The ReAct recipe calls recall before "think" and splices the result into the
system prompt — the kernel still has no idea memory exists.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field


@dataclass
class MemoryRecord:
    content: str
    tags: list[str] = field(default_factory=list)
    importance: float = 1.0
    record_id: str = ""


def _tokens(text: str) -> set[str]:
    # Teaching build: split English on words and Chinese on single characters,
    # which is enough to demonstrate relevance ranking.
    return set(re.findall(r"[a-zA-Z]+|[\u4e00-\u9fff]", text.lower()))


class InMemoryMemory:
    def __init__(self):
        self._records: list[MemoryRecord] = []
        self._seq = 0

    async def remember(
        self, content: str, *, tags: list[str] | None = None, importance: float = 1.0
    ) -> MemoryRecord:
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
