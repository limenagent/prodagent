"""File-backed ``DocumentStore`` — JSON file for CONSTRAINT/PREFERENCE/EPISODIC memories."""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

from prodagent.backends.file._locking import _exclusive
from prodagent.cognition.memory.storage import (
    EPISODIC_DEFAULT_TTL_DAYS,
    MAX_SOFT_MEMORIES,
    MemoryRecord,
    MemoryType,
    StoredMemory,
    mem_id,
)
from prodagent.core.io import write_atomic_json
from prodagent.core.time import now_timestamp

logger = logging.getLogger(__name__)

__all__ = ["FileDocumentStore"]


def _read_json(path: Path, *, default: Any) -> Any:
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        logger.warning("[memory] read failed for %s: %s — treating as empty", path, exc)
        return default


def _write_json(path: Path, data: Any) -> None:
    write_atomic_json(path, data, fsync=False)


class FileDocumentStore:
    """JSON-file-backed ``DocumentStore``."""

    def __init__(self, directory: str | Path) -> None:
        self._dir = Path(directory)
        self._dir.mkdir(parents=True, exist_ok=True)

    @property
    def _memories_file(self) -> Path:
        return self._dir / "memories_soft.json"

    @property
    def _lock_file(self) -> Path:
        return self._dir / "memories_soft.lock"

    def load_constraints(self) -> list[StoredMemory]:
        """Filtered view over the soft-memory pool, so ``RuleChannel``
        force-recalls constraints only."""
        return [m for m in self.load_memories() if m.memory_type is MemoryType.CONSTRAINT]

    def load_memories(self) -> list[StoredMemory]:
        return [StoredMemory.from_dict(m) for m in _read_json(self._memories_file, default=[])]

    def save_memories(self, data: list[StoredMemory]) -> None:
        with _exclusive(self._lock_file):
            _write_json(self._memories_file, [m.to_dict() for m in data[:MAX_SOFT_MEMORIES]])

    def append_soft(self, record: MemoryRecord) -> None:
        ttl = record.ttl_days
        if ttl is None and record.memory_type is MemoryType.EPISODIC:
            ttl = EPISODIC_DEFAULT_TTL_DAYS

        stored = StoredMemory.from_record(
            record,
            id=mem_id(record.content),
            created_at=now_timestamp(),
        )
        stored.ttl_days = ttl
        with _exclusive(self._lock_file):
            memories = [
                StoredMemory.from_dict(m) for m in _read_json(self._memories_file, default=[])
            ]
            memories.insert(0, stored)
            _write_json(self._memories_file, [m.to_dict() for m in memories[:MAX_SOFT_MEMORIES]])

    def mark_superseded(self, mem_id: str, superseded: bool) -> None:
        with _exclusive(self._lock_file):
            memories = [
                StoredMemory.from_dict(m) for m in _read_json(self._memories_file, default=[])
            ]
            for mem in memories:
                if mem.id == mem_id:
                    mem.superseded = superseded
                    _write_json(self._memories_file, [m.to_dict() for m in memories])
                    return

    def touch_memory(self, mem_id: str) -> None:
        with _exclusive(self._lock_file):
            memories = [
                StoredMemory.from_dict(m) for m in _read_json(self._memories_file, default=[])
            ]
            for mem in memories:
                if mem.id == mem_id:
                    mem.access_count += 1
                    mem.last_access = now_timestamp()
                    _write_json(self._memories_file, [m.to_dict() for m in memories])
                    return
