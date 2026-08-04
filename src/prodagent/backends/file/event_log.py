"""File-based event log — durable JSONL, one file per plan_id."""

from __future__ import annotations

import asyncio
import json
import logging
import os
from pathlib import Path

from prodagent.backends.file._locking import _exclusive
from prodagent.core.event_log import Event, PlanEventType
from prodagent.core.exceptions import VersionConflict
from prodagent.core.io import read_jsonl, safe_filename_component

logger = logging.getLogger(__name__)

__all__ = ["FileEventLog"]


def _count_valid_lines(path: Path) -> int:
    if not path.exists():
        return 0
    return sum(1 for _ in read_jsonl(path))


def _read_tail_seq(path: Path) -> int:
    """Return the ``seq`` of the last complete JSON line in *path*."""
    if not path.exists():
        return 0
    size = path.stat().st_size
    if size == 0:
        return 0
    chunk_size = min(size, 8192)
    pos = size
    while True:
        start = max(0, pos - chunk_size)
        with path.open("rb") as f:
            f.seek(start)
            chunk = f.read(size - start)
        for line in reversed(chunk.split(b"\n")):
            if not line.strip():
                continue
            try:
                d = json.loads(line)
            except json.JSONDecodeError:
                continue
            seq = d.get("seq")
            if isinstance(seq, int):
                return seq
        if start == 0:
            return 0  # whole file scanned, nothing parseable
        pos = start


class FileEventLog:
    """Durable JSONL event log.

    A crash mid-write leaves at most one corrupt trailing line, which ``_load``
    skips.
    """

    def __init__(self, directory: str | Path, *, fsync: bool = False) -> None:
        self._dir = Path(directory)
        self._dir.mkdir(parents=True, exist_ok=True)
        self._fsync = fsync

    def _path(self, plan_id: str) -> Path:
        return self._dir / f"{safe_filename_component(plan_id)}.jsonl"

    def _lock_path(self, plan_id: str) -> Path:
        return self._dir / f"{safe_filename_component(plan_id)}.lock"

    def _append_sync(self, event: Event, expected_seq: int | None) -> int:
        path = self._path(event.plan_id)
        with _exclusive(self._lock_path(event.plan_id)):
            current = _read_tail_seq(path)
            if expected_seq is not None and current != expected_seq:
                raise VersionConflict(
                    f"expected tail seq {expected_seq} for plan {event.plan_id}, "
                    f"found {current} — concurrent writer won"
                )
            event.seq = current + 1
            record = {
                "seq": event.seq,
                "event_id": event.event_id,
                "event_type": event.event_type,
                "plan_id": event.plan_id,
                "version": event.version,
                "timestamp": event.timestamp,
                "data": event.data,
            }
            with path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(record, ensure_ascii=False) + "\n")
                f.flush()
                if self._fsync:
                    os.fsync(f.fileno())
            return event.seq

    async def append(self, event: Event, expected_seq: int | None = None) -> int:
        return await asyncio.to_thread(self._append_sync, event, expected_seq)

    def _load(self, plan_id: str) -> list[Event]:
        path = self._path(plan_id)
        if not path.exists():
            return []
        events: list[Event] = []
        for d in read_jsonl(path):
            try:
                events.append(
                    Event(
                        seq=d["seq"],
                        event_id=d["event_id"],
                        event_type=PlanEventType(d["event_type"]),
                        plan_id=d["plan_id"],
                        version=d["version"],
                        timestamp=d["timestamp"],
                        data=d["data"],
                    )
                )
            except KeyError:
                logger.warning("[event_log] skipping corrupt line in %s", path.name)
        return events

    async def get_events(self, plan_id: str) -> list[Event]:
        return await asyncio.to_thread(self._load, plan_id)

    async def get_after(self, plan_id: str, since_seq: int) -> list[Event]:
        events = await asyncio.to_thread(self._load, plan_id)
        return [e for e in events if e.seq > since_seq]
