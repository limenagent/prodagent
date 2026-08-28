"""DocumentStore port — storage contract and data model for CONSTRAINT/PREFERENCE/EPISODIC memories."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Protocol, runtime_checkable

from prodagent.base.codec import dump, load

__all__ = [
    "DocumentStore",
    "MemoryType",
    "MemoryRecord",
    "StoredMemory",
    "MAX_SOFT_MEMORIES",
    "EPISODIC_DEFAULT_TTL_DAYS",
    "mem_id",
]

MAX_SOFT_MEMORIES = 300
EPISODIC_DEFAULT_TTL_DAYS = 7


def mem_id(text: str, *, prefix: str = "") -> str:
    h = hashlib.blake2b(text.encode(), digest_size=6).hexdigest()
    return f"{prefix}{h}" if prefix else h


class MemoryType(StrEnum):
    CONSTRAINT = "constraint"
    FACT = "fact"
    PREFERENCE = "preference"
    EPISODIC = "episodic"


@dataclass
class MemoryRecord:
    """Write-side DTO — what a Classifier produces and a Store consumes."""

    content: str
    memory_type: MemoryType = MemoryType.EPISODIC
    entity_id: str = ""
    domain: str = "general"
    ttl_days: int | None = None
    source: str = ""
    embedding: list[float] | None = None

    def __post_init__(self) -> None:
        self.memory_type = _coerce_memory_type(self.memory_type)


@dataclass
class StoredMemory:
    """Persisted record — what lives in memories.json and what recall returns."""

    id: str
    content: str
    memory_type: MemoryType
    domain: str = "general"
    entity_id: str = ""
    ttl_days: int | None = None
    created_at: str = ""
    superseded: bool = False
    version: int = 1
    access_count: int = 0
    last_access: str = ""
    embedding: list[float] | None = None

    def __post_init__(self) -> None:
        self.memory_type = _coerce_memory_type(self.memory_type)

    @classmethod
    def from_record(cls, record: MemoryRecord, *, id: str, created_at: str) -> StoredMemory:
        return cls(
            id=id,
            content=record.content,
            memory_type=record.memory_type,
            domain=record.domain,
            entity_id=record.entity_id,
            ttl_days=record.ttl_days,
            created_at=created_at,
            embedding=record.embedding,
        )

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> StoredMemory:
        return load(
            cls,
            d,
            defaults={"id": "", "content": "", "memory_type": MemoryType.EPISODIC.value},
        )

    def to_dict(self) -> dict[str, Any]:
        return dump(self)


def _coerce_memory_type(value: Any) -> MemoryType:
    if isinstance(value, MemoryType):
        return value
    return MemoryType(str(value).lower())


@runtime_checkable
class DocumentStore(Protocol):
    """Storage for CONSTRAINT + PREFERENCE + EPISODIC memories."""

    async def load_constraints(self) -> list[StoredMemory]: ...
    async def load_memories(self) -> list[StoredMemory]: ...
    async def save_memories(self, data: list[StoredMemory]) -> None: ...
    async def append_soft(self, record: MemoryRecord) -> None: ...
    async def mark_superseded(self, mem_id: str, superseded: bool) -> None: ...
    async def touch_memory(self, mem_id: str) -> None: ...
