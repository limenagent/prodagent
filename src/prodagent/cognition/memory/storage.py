from __future__ import annotations

import hashlib
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

__all__ = [
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
        return cls(
            id=d.get("id", ""),
            content=d.get("content", ""),
            memory_type=d.get("memory_type", "episodic"),
            domain=d.get("domain", "general"),
            entity_id=d.get("entity_id", ""),
            ttl_days=d.get("ttl_days"),
            created_at=d.get("created_at", ""),
            superseded=d.get("superseded", False),
            version=d.get("version", 1),
            access_count=d.get("access_count", 0),
            last_access=d.get("last_access", ""),
            embedding=d.get("embedding"),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "content": self.content,
            "memory_type": self.memory_type.value,
            "domain": self.domain,
            "entity_id": self.entity_id,
            "ttl_days": self.ttl_days,
            "created_at": self.created_at,
            "superseded": self.superseded,
            "version": self.version,
            "access_count": self.access_count,
            "last_access": self.last_access,
            "embedding": self.embedding,
        }


def _coerce_memory_type(value: Any) -> MemoryType:
    if isinstance(value, MemoryType):
        return value
    return MemoryType(str(value).lower())
