"""Memory storage model — re-exported from the port layer (prodagent.ports.persistence)."""

from __future__ import annotations

from prodagent.ports.persistence import (
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
