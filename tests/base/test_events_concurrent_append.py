from __future__ import annotations

import asyncio
import json

from prodagent.backends.file.event_log import FileEventLog, _read_tail_seq
from prodagent.base.errors import VersionConflict
from prodagent.base.event_log import Event, PlanEventType


def _make(
    event_type: PlanEventType = PlanEventType.STEP_COMPLETED, stream_id: str = "p1", **data
) -> Event:
    return Event.make(event_type, stream_id, version=1, **data)


class TestConcurrentAppend:
    async def test_concurrent_appends_do_not_interleave(self, tmp_path):
        log = FileEventLog(tmp_path)
        n_coros = 8
        n_appends_per_coro = 20

        async def worker(tid: int) -> None:
            for i in range(n_appends_per_coro):
                await log.append(_make(step_id=f"t{tid}-s{i}"))

        await asyncio.gather(*(worker(tid) for tid in range(n_coros)))

        path = log._path("p1")
        lines = path.read_text(encoding="utf-8").splitlines()
        expected = n_coros * n_appends_per_coro
        assert len(lines) == expected, (
            f"expected {expected} lines, got {len(lines)} — "
            "concurrent appends interleaved or dropped"
        )

        seqs = []
        for line in lines:
            d = json.loads(line)
            seqs.append(d["seq"])
        assert sorted(seqs) == list(range(1, expected + 1)), (
            "seqs must be 1..N with no gaps or duplicates"
        )

    async def test_expected_seq_race_loser_gets_version_conflict(self, tmp_path):
        log = FileEventLog(tmp_path)
        await log.append(_make(PlanEventType.PLAN_CREATED))

        async def worker(tid: int) -> str:
            try:
                await log.append(_make(step_id=f"s{tid}"), expected_seq=1)
                return "ok"
            except VersionConflict:
                return "conflict"

        results = await asyncio.gather(worker(1), worker(2))
        assert sorted(results) == ["conflict", "ok"], (
            f"expected one ok + one conflict, got {results}"
        )

    async def test_read_tail_seq_is_o_last_line_not_o_file(self, tmp_path):
        log = FileEventLog(tmp_path)
        for i in range(1000):
            await log.append(_make(step_id=f"s{i}"))

        path = log._path("p1")
        assert _read_tail_seq(path) == 1000

    async def test_read_tail_seq_skips_corrupt_trailing_line(self, tmp_path):
        log = FileEventLog(tmp_path)
        await log.append(_make(PlanEventType.PLAN_CREATED))
        await log.append(_make(step_id="s1"))

        path = log._path("p1")
        with path.open("a", encoding="utf-8") as f:
            f.write('{"seq": 99, "partial":')

        assert _read_tail_seq(path) == 2, "must skip corrupt line and return last valid seq"

    async def test_read_tail_seq_zero_for_empty_or_missing(self, tmp_path):
        log = FileEventLog(tmp_path)
        path = log._path("never-written")
        assert _read_tail_seq(path) == 0

        path.write_text("")
        assert _read_tail_seq(path) == 0

    async def test_flock_does_not_deadlock_single_process_reentrant(self, tmp_path):
        log = FileEventLog(tmp_path)
        for i in range(500):
            seq = await log.append(_make(step_id=f"s{i}"))
            assert seq == i + 1
