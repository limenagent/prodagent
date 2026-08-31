"""File-based event log — durable JSONL, one file per stream_id."""

from __future__ import annotations

import asyncio
import json
import logging
import os
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

from prodagent.backends._shared.tailing import StreamWakes, tail_stream
from prodagent.backends.file._locking import _exclusive
from prodagent.base.errors import VersionConflict
from prodagent.base.event_log import Event
from prodagent.base.io import read_jsonl, safe_filename_component

logger = logging.getLogger(__name__)

__all__ = ["FileEventLog"]


def _count_valid_lines(path: Path) -> int:
    if not path.exists():
        return 0
    return sum(1 for _ in read_jsonl(path))


def _read_tail_seq(path: Path) -> int:
    """Return the ``seq`` of the last complete JSON line in *path*.

    Scans backwards in 8KB chunks — this runs on every append, so paying
    O(file) per append would make long streams quadratically slow."""
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
        self._wakes = StreamWakes()

    def _path(self, stream_id: str) -> Path:
        return self._dir / f"{safe_filename_component(stream_id)}.jsonl"

    def _lock_path(self, stream_id: str) -> Path:
        return self._dir / f"{safe_filename_component(stream_id)}.lock"

    def _append_batch_sync(self, events: list[Event], expected_seq: int | None) -> list[int]:
        """Group commit: one lock/tail-scan/open/flush per stream for the
        whole batch — the amortization that makes write-behind pipelines
        cheap. Batch order within a stream is input order."""
        # Group by stream, preserving first-appearance order for deterministic
        # lock acquisition (mixed-stream batches come from the buffered tier).
        grouped: dict[str, list[Event]] = {}
        for event in events:
            grouped.setdefault(event.stream_id, []).append(event)
        checked: set[str] = set()
        for stream_id, stream_events in grouped.items():
            path = self._path(stream_id)
            with _exclusive(self._lock_path(stream_id)):
                current = _read_tail_seq(path)
                if stream_id not in checked:
                    if expected_seq is not None and current != expected_seq:
                        raise VersionConflict(
                            f"expected tail seq {expected_seq} for stream {stream_id}, "
                            f"found {current} — concurrent writer won"
                        )
                    checked.add(stream_id)
                records = []
                for event in stream_events:
                    event.seq = current + 1
                    current = event.seq
                    records.append(
                        {
                            "seq": event.seq,
                            "event_id": event.event_id,
                            "event_type": event.event_type,
                            "stream_id": event.stream_id,
                            "version": event.version,
                            "timestamp": event.timestamp,
                            "data": event.data,
                        }
                    )
                with path.open("a", encoding="utf-8") as f:
                    f.writelines(json.dumps(r, ensure_ascii=False) + "\n" for r in records)
                    f.flush()
                    if self._fsync:
                        os.fsync(f.fileno())
        return [e.seq for e in events]

    async def append_events(
        self, events: list[Event], expected_seq: int | None = None
    ) -> list[int]:
        if not events:
            return []
        seqs = await asyncio.to_thread(self._append_batch_sync, events, expected_seq)
        for stream_id in {e.stream_id for e in events}:
            self._wakes.notify(stream_id)
        return seqs

    async def append(self, event: Event, expected_seq: int | None = None) -> int:
        return (await self.append_events([event], expected_seq))[0]

    def _load(self, stream_id: str) -> list[Event]:
        path = self._path(stream_id)
        if not path.exists():
            return []
        events: list[Event] = []
        for d in read_jsonl(path):
            try:
                events.append(
                    Event(
                        seq=d["seq"],
                        event_id=d["event_id"],
                        event_type=d["event_type"],
                        stream_id=d["stream_id"],
                        version=d["version"],
                        timestamp=d["timestamp"],
                        data=d["data"],
                    )
                )
            except KeyError:
                logger.warning("[event_log] skipping corrupt line in %s", path.name)
        return events

    async def get_events(self, stream_id: str) -> list[Event]:
        return await asyncio.to_thread(self._load, stream_id)

    async def get_after(self, stream_id: str, since_seq: int) -> list[Event]:
        events = await asyncio.to_thread(self._load, stream_id)
        return [e for e in events if e.seq > since_seq]

    async def list_streams(self) -> list[str]:
        def _scan() -> list[str]:
            return [p.stem for p in self._dir.glob("*.jsonl") if p.stat().st_size > 0]

        return await asyncio.to_thread(_scan)

    def subscribe(self, stream_id: str, since_seq: int = 0) -> AsyncIterator[Event]:
        # In-process appends wake immediately; the poll fallback in
        # ``tail_stream`` catches writers in other processes sharing the
        # same directory.
        return tail_stream(self.get_after, self._wakes, stream_id, since_seq)

    async def replicate(self, events: list[Event]) -> None:
        if not events:
            return
        for event in events:
            if event.seq < 1:
                # Event.make leaves seq=0 as the "unassigned" placeholder —
                # shipping one means the source never sequenced it (a wiring
                # bug), and silently skipping it would lose a fact.
                raise ValueError(
                    f"cannot replicate an unsequenced event ({event.event_type} on "
                    f"{event.stream_id}) — append it through a log first"
                )
        await asyncio.to_thread(self._replicate_sync, events)
        for stream_id in {e.stream_id for e in events}:
            self._wakes.notify(stream_id)

    def _replicate_sync(self, events: list[Event]) -> None:
        """Absorb at the events' own seqs under the stream lock — the tail
        scan skips what is already durably there (idempotent re-ship), the
        rest lands in order with their original seqs, one write per stream."""
        grouped: dict[str, list[Event]] = {}
        for event in events:
            grouped.setdefault(event.stream_id, []).append(event)
        for stream_id, stream_events in grouped.items():
            path = self._path(stream_id)
            with _exclusive(self._lock_path(stream_id)):
                tail = _read_tail_seq(path)
                lines = []
                for event in stream_events:
                    if event.seq <= tail:
                        continue  # already absorbed — idempotent re-ship
                    lines.append(
                        json.dumps(
                            {
                                "seq": event.seq,
                                "event_id": event.event_id,
                                "event_type": event.event_type,
                                "stream_id": event.stream_id,
                                "version": event.version,
                                "timestamp": event.timestamp,
                                "data": event.data,
                            },
                            ensure_ascii=False,
                        )
                        + "\n"
                    )
                    tail = event.seq
                if lines:
                    with path.open("a", encoding="utf-8") as f:
                        f.writelines(lines)
                        f.flush()
                        if self._fsync:
                            os.fsync(f.fileno())
