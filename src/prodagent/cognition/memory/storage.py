"""Memory storage model — re-exported from the port layer (prodagent.ports.document)."""

from __future__ import annotations

from prodagent.ports.document import (
    EPISODIC_DEFAULT_TTL_DAYS,
    MAX_SOFT_MEMORIES,
    MemoryRecord,
    MemoryType,
    StoredMemory,
    mem_id,
)

__all__ = [
    "MemoryType",
    "MemoryRecord",
    "StoredMemory",
    "MAX_SOFT_MEMORIES",
    "EPISODIC_DEFAULT_TTL_DAYS",
    "mem_id",
]
