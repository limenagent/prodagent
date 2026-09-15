"""file_store — local-file checkpoints and event log for cross-process resume.

The kernel only defines the CheckpointStore / EventLog ports and ships in-memory
defaults. Here is the file-backed version:
- checkpoints: one .json per run; writes use "temp file + os.replace" atomic
  replacement, so a crash at any instant never leaves a half-written checkpoint;
- event log: one .jsonl per run, append-only — a natural audit trail.

Swapping in Redis/Postgres is just writing two more classes that satisfy the
same protocols; the kernel and recipes don't change a line.
"""

from __future__ import annotations

import json
import os
import pathlib

from src.kernel import Event


def _atomic_write_json(path: pathlib.Path, obj) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(obj, ensure_ascii=False, default=str), encoding="utf-8")
    os.replace(tmp, path)  # same-directory rename is atomic on both POSIX and NT


class FileCheckpointStore:
    def __init__(self, directory: str):
        self.dir = pathlib.Path(directory)
        self.dir.mkdir(parents=True, exist_ok=True)

    def _path(self, run_id: str) -> pathlib.Path:
        return self.dir / f"{run_id}.json"

    async def save(
        self, run_id: str, snapshot: dict, *, expected_version: int | None = None
    ) -> int:
        path = self._path(run_id)
        version = 0
        if path.exists():
            version = json.loads(path.read_text(encoding="utf-8")).get("_version", 0)
        if expected_version is not None and version != expected_version:
            raise RuntimeError(
                f"checkpoint version conflict: expected {expected_version}, got {version}"
            )
        version += 1
        record = dict(snapshot)
        record["_version"] = version
        _atomic_write_json(path, record)
        return version

    async def load(self, run_id: str) -> dict | None:
        path = self._path(run_id)
        if not path.exists():
            return None
        record = json.loads(path.read_text(encoding="utf-8"))
        record.pop("_version", None)
        return record


class FileEventLog:
    def __init__(self, directory: str):
        self.dir = pathlib.Path(directory)
        self.dir.mkdir(parents=True, exist_ok=True)

    def _path(self, run_id: str) -> pathlib.Path:
        return self.dir / f"{run_id}.jsonl"

    async def append(self, event: Event) -> int:
        line = json.dumps(
            {
                "seq": event.seq,
                "run_id": event.run_id,
                "kind": event.kind,
                "data": event.data,
                "parent_id": event.parent_id,
            },
            ensure_ascii=False,
            default=str,
        )
        with self._path(event.run_id).open("a", encoding="utf-8") as f:
            f.write(line + "\n")
        return event.seq

    def _read(self, run_id: str) -> list[Event]:
        path = self._path(run_id)
        if not path.exists():
            return []
        events = []
        # Split on "\n" only: splitlines() would also cut on U+0085/U+2028/U+2029,
        # which json.dumps(ensure_ascii=False) writes raw — a record boundary must
        # never fall inside a JSON string.
        for line in path.read_text(encoding="utf-8").split("\n"):
            if not line.strip():
                continue
            d = json.loads(line)
            events.append(
                Event(d["seq"], d["run_id"], d["kind"], d.get("data", {}), d.get("parent_id"))
            )
        return events

    async def events(self, run_id: str) -> list[Event]:
        return self._read(run_id)
