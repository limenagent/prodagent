"""In-process LRU LLM response cache."""

from __future__ import annotations

from collections import OrderedDict
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from prodagent.kernel.types import LLMResponse


class InMemoryCache:
    """Bounded LRU cache. One instance safely serves many concurrent runs."""

    def __init__(self, *, max_entries: int = 1024) -> None:
        self._entries: OrderedDict[str, LLMResponse] = OrderedDict()
        self._max = max(1, max_entries)

    async def get(self, key: str) -> LLMResponse | None:
        if key not in self._entries:
            return None
        self._entries.move_to_end(key)
        return self._entries[key]

    async def set(self, key: str, response: LLMResponse) -> None:
        self._entries[key] = response
        self._entries.move_to_end(key)
        while len(self._entries) > self._max:
            self._entries.popitem(last=False)
