"""Shared ``append_soft`` construction logic for file/postgres ``DocumentStore``s."""

from __future__ import annotations

from prodagent.core.time import now_timestamp
from prodagent.ports.document import (
    EPISODIC_DEFAULT_TTL_DAYS,
    MemoryRecord,
    MemoryType,
    StoredMemory,
    mem_id,
)


def build_stored_memory(record: MemoryRecord) -> StoredMemory:
    ttl = record.ttl_days
    if ttl is None and record.memory_type is MemoryType.EPISODIC:
        ttl = EPISODIC_DEFAULT_TTL_DAYS

    stored = StoredMemory.from_record(
        record,
        id=mem_id(record.content),
        created_at=now_timestamp(),
    )
    stored.ttl_days = ttl
    return stored
