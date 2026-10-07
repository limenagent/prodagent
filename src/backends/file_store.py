"""file_store — the local-file EventLog: the .jsonl IS the database.

The kernel defines only the EventLog port and ships an in-memory default; this
is the file-backed version for cross-process resume. One append-only .jsonl per
run — a natural audit trail and the single durable thing a resume needs: the
next process replays it and continues (see kernel.replay). Swapping in
Redis/Postgres is one more class satisfying the same protocol; the kernel and
recipes don't change a line.
"""

from __future__ import annotations

import json
import pathlib

from src.kernel import Event


def _assert_native(value: object, path: str) -> None:
    """Event payloads must be JSON-native (dict/list/str/number/bool/None).

    The log is the only durable thing; a live object serialized via
    default=str would silently become a repr string — unreadable on replay.
    Fail at the boundary, where the fact is made, never at the reader.
    """
    if isinstance(value, dict):
        for k, v in value.items():
            _assert_native(v, f"{path}.{k}")
    elif isinstance(value, (list, tuple)):
        for i, v in enumerate(value):
            _assert_native(v, f"{path}[{i}]")
    elif value is not None and not isinstance(value, (str, int, float, bool)):
        raise TypeError(
            f"event data must be JSON-native at {path}: got {type(value).__name__}; "
            "encode live objects at the source (the log is the only truth)"
        )


class FileEventLog:
    def __init__(self, directory: str):
        self.dir = pathlib.Path(directory)
        self.dir.mkdir(parents=True, exist_ok=True)

    def _path(self, run_id: str) -> pathlib.Path:
        return self.dir / f"{run_id}.jsonl"

    async def append(self, event: Event) -> None:
        _assert_native(event.data, "data")  # loud now, not a silent repr later
        line = json.dumps(
            {
                "seq": event.seq,
                "run_id": event.run_id,
                "kind": event.kind,
                "data": event.data,
                "parent_id": event.parent_id,
                "ts": event.ts,
            },
            ensure_ascii=False,
            default=str,
        )
        with self._path(event.run_id).open("a", encoding="utf-8") as f:
            f.write(line + "\n")

    def _read(self, run_id: str) -> list[Event]:
        path = self._path(run_id)
        if not path.exists():
            return []
        events = []
        # Split on "\n" only: splitlines() would also cut on U+0085/U+2028/U+2029,
        # which json.dumps(ensure_ascii=False) writes raw — a record boundary must
        # never fall inside a JSON string.
        lines = path.read_text(encoding="utf-8").split("\n")
        for i, line in enumerate(lines):
            if not line.strip():
                continue
            try:
                d = json.loads(line)
            except json.JSONDecodeError:
                if i == len(lines) - 1:
                    # A torn final line is an append that crashed mid-write: it
                    # never completed, so it is not a fact — skip it and the
                    # log replays up to the last durable fact.
                    break
                # A bad line anywhere else is real corruption; failing loudly
                # beats silently dropping a fact the replay would then miss.
                raise
            # ts round-trips with the record; a log written before ts existed
            # reads as 0.0 — an unknown duration, never a fake read-time clock.
            events.append(
                Event(
                    d["seq"],
                    d["run_id"],
                    d["kind"],
                    d.get("data", {}),
                    d.get("parent_id"),
                    ts=d.get("ts", 0.0),
                )
            )
        return events

    async def events(self, run_id: str) -> list[Event]:
        return self._read(run_id)

    async def all_events(self) -> list[Event]:
        out: list[Event] = []
        for path in sorted(self.dir.glob("*.jsonl")):
            out.extend(self._read(path.stem))
        return out
