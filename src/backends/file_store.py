"""file_store —— 用本地文件实现检查点与事件日志，真正做到跨进程断点续跑。

内核只定义 CheckpointStore / EventLog 两个端口，默认给内存版。这里给出文件版：
- 检查点：每个 run 一个 .json，写入走“临时文件 + os.replace”原子替换，
  进程在任意时刻崩溃都不会留下写了一半的检查点；
- 事件日志：每个 run 一个 .jsonl，只追加，天然是审计流水。

换成 Redis/Postgres 只是再写两个满足同样协议的类，内核与配方一行不改。
"""

from __future__ import annotations

import json
import os
import pathlib

from src.kernel import Event


def _atomic_write_json(path: pathlib.Path, obj) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(obj, ensure_ascii=False, default=str), encoding="utf-8")
    os.replace(tmp, path)  # 同目录 rename 在 POSIX/NT 上都是原子的


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
            raise RuntimeError(f"检查点版本冲突：期望 {expected_version}，实际 {version}")
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
        for line in path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            d = json.loads(line)
            events.append(
                Event(d["seq"], d["run_id"], d["kind"], d.get("data", {}), d.get("parent_id"))
            )
        return events

    async def events(self, run_id: str) -> list[Event]:
        return self._read(run_id)

    async def after(self, run_id: str, since_seq: int) -> list[Event]:
        return [e for e in self._read(run_id) if e.seq > since_seq]
