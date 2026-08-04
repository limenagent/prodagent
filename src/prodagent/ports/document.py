"""DocumentStore port — storage for CONSTRAINT + PREFERENCE + EPISODIC memories."""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol, runtime_checkable

if TYPE_CHECKING:
    from prodagent.cognition.memory.storage import MemoryRecord, StoredMemory


@runtime_checkable
class DocumentStore(Protocol):
    """Storage for CONSTRAINT + PREFERENCE + EPISODIC memories."""

    def load_constraints(self) -> list[StoredMemory]: ...
    def load_memories(self) -> list[StoredMemory]: ...
    def save_memories(self, data: list[StoredMemory]) -> None: ...
    def append_soft(self, record: MemoryRecord) -> None: ...
    def mark_superseded(self, mem_id: str, superseded: bool) -> None: ...
    def touch_memory(self, mem_id: str) -> None: ...
