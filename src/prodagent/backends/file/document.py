"""File-backed ``DocumentStore`` — JSON file for CONSTRAINT/PREFERENCE/EPISODIC memories."""

from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Callable

from prodagent.backends._shared.document_write import build_stored_memory
from prodagent.backends.file._locking import _exclusive
from prodagent.core.io import write_atomic_json
from prodagent.core.time import now_timestamp
from prodagent.ports.document import (
    MAX_SOFT_MEMORIES,
    MemoryRecord,
    MemoryType,
    StoredMemory,
)

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

    async def load_constraints(self) -> list[StoredMemory]:
        """Filtered view over the soft-memory pool, so ``RuleChannel``
        force-recalls constraints only."""
        return [m for m in await self.load_memories() if m.memory_type is MemoryType.CONSTRAINT]

    async def load_memories(self) -> list[StoredMemory]:
        return await asyncio.to_thread(
            lambda: [StoredMemory.from_dict(m) for m in _read_json(self._memories_file, default=[])]
        )

    async def save_memories(self, data: list[StoredMemory]) -> None:
        def _save() -> None:
            with _exclusive(self._lock_file):
                _write_json(self._memories_file, [m.to_dict() for m in data[:MAX_SOFT_MEMORIES]])

        await asyncio.to_thread(_save)

    async def append_soft(self, record: MemoryRecord) -> None:
        def _append() -> None:
            stored = build_stored_memory(record)
            with _exclusive(self._lock_file):
                memories = [
                    StoredMemory.from_dict(m) for m in _read_json(self._memories_file, default=[])
                ]
                memories.insert(0, stored)
                _write_json(
                    self._memories_file, [m.to_dict() for m in memories[:MAX_SOFT_MEMORIES]]
                )

        await asyncio.to_thread(_append)

    async def _mutate_mem(self, mem_id: str, fn: Callable[[StoredMemory], None]) -> None:
        def _mutate() -> None:
            with _exclusive(self._lock_file):
                memories = [
                    StoredMemory.from_dict(m) for m in _read_json(self._memories_file, default=[])
                ]
                for mem in memories:
                    if mem.id == mem_id:
                        fn(mem)
                        _write_json(self._memories_file, [m.to_dict() for m in memories])
                        return

        await asyncio.to_thread(_mutate)

    async def mark_superseded(self, mem_id: str, superseded: bool) -> None:
        await self._mutate_mem(mem_id, lambda mem: setattr(mem, "superseded", superseded))

    async def touch_memory(self, mem_id: str) -> None:
        def _touch(mem: StoredMemory) -> None:
            mem.access_count += 1
            mem.last_access = now_timestamp()

        await self._mutate_mem(mem_id, _touch)
