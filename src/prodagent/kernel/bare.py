"""The kernel's own in-process IO pair — the bare-profile default.

State dies with the process; durability arrives by injecting a real
backend (``backends/*``). The kernel implements these itself, not as a
courtesy: graph tracking always wires a (log, store) pair, and the kernel
cannot import a capability package to get one — the same
port-implementation precedent as ``BudgetLedger``. The contracts are the
durable backends': per-stream monotonic seq, optimistic tail check, one
snapshot per run with an optimistic version check.
"""

from __future__ import annotations

import contextlib
from typing import Any

__all__ = ["BareEventLog", "BareCheckpointStore"]


class BareEventLog:
    """In-process event log — per-stream monotonic seq, optimistic tail
    check, BASE capabilities only (``subscribe`` replays what exists and
    stops — nothing wakes on append)."""

    def __init__(self) -> None:
        self._streams: dict[str, list[Any]] = {}

    async def append(self, event: Any, expected_seq: int | None = None) -> int:
        return (await self.append_events([event], expected_seq))[0]

    async def append_events(self, events: list[Any], expected_seq: int | None = None) -> list[int]:
        return await self._append_batch(list(events), expected_seq)

    async def _append_batch(self, events: list[Any], expected_seq: int | None) -> list[int]:
        if not events:
            return []
        stream_id = events[0].stream_id
        stream = self._streams.setdefault(stream_id, [])
        if expected_seq is not None and len(stream) != expected_seq:
            from prodagent.base.errors import VersionConflict

            raise VersionConflict(
                f"expected tail seq {expected_seq} for stream {stream_id}, "
                f"found {len(stream)} — concurrent writer won"
            )
        # Seq convention: the tail check counts events (0 = empty), and an
        # appended event's seq is its 1-based position — tail and count agree.
        seqs = []
        for event in events:
            stream.append(event)
            with contextlib.suppress(AttributeError):  # frozen event: seq kept by position
                event.seq = len(stream)
            seqs.append(len(stream))
        return seqs

    async def get_events(self, stream_id: str) -> list[Any]:
        return list(self._streams.get(stream_id, ()))

    async def get_after(self, stream_id: str, *, since_seq: int) -> list[Any]:
        out = []
        for i, e in enumerate(self._streams.get(stream_id, ())):
            seq = getattr(e, "seq", i + 1)
            if seq > since_seq:
                out.append(e)
        return out

    async def subscribe(self, stream_id: str) -> Any:
        for event in list(self._streams.get(stream_id, ())):
            yield event


class BareCheckpointStore:
    """In-process checkpoint store — one snapshot per run (latest wins),
    optimistic version check preserved: the discipline is the same, only
    the durability is missing."""

    def __init__(self) -> None:
        self._runs: dict[str, Any] = {}
        self._versions: dict[str, int] = {}

    async def save(self, run: Any, expected_version: int | None = None) -> None:
        from prodagent.base.errors import VersionConflict

        stored = self._versions.get(run.run_id, 0)
        if expected_version is not None and stored != expected_version:
            raise VersionConflict(
                f"checkpoint version mismatch for run={run.run_id}: "
                f"expected {expected_version}, stored {stored}"
            )
        self._runs[run.run_id] = run
        self._versions[run.run_id] = stored + 1
        run.checkpoint_version = stored + 1

    async def load(self, run_id: str, version: int | None = None) -> Any | None:
        run = self._runs.get(run_id)
        if run is not None and version is not None and self._versions.get(run_id) != version:
            return None  # bare keeps only the latest — an old version is absent
        return run

    async def list_run_ids(self) -> list[str]:
        return list(self._runs)
