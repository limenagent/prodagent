from __future__ import annotations

import pytest

from prodagent.backends.file.checkpoint import FileCheckpointStore
from prodagent.backends.file.event_log import FileEventLog
from prodagent.base.event_log import (
    Event,
    PlanEventType,
    hybrid_restore,
)
from prodagent.kernel.event_log import apply_event
from prodagent.kernel.run import Run


def _make(event_type: PlanEventType, stream_id: str = "p1", version: int = 1, **data) -> Event:
    return Event.make(event_type, stream_id, version, **data)


def _extract_base(run: Run) -> tuple[dict, int, int] | None:
    tail = run.cursor("plan") or {}
    return (
        (tail.get("state"), run.checkpoint_version, tail.get("last_seq", 0))
        if tail.get("state")
        else None
    )


def _empty_state() -> dict:
    return {"nodes": {}, "version": 0}


def _simple_reducer(state: dict, event: Event) -> dict:
    apply_event(state, event)
    return state


class TestEventSeq:
    def test_make_sets_seq_zero(self):
        e = _make(PlanEventType.PLAN_CREATED)
        assert e.seq == 0, "seq must be 0 until FileEventLog.append() assigns it"

    async def test_append_assigns_monotonic_seq(self, tmp_path):
        log = FileEventLog(tmp_path)
        e1 = _make(PlanEventType.PLAN_CREATED)
        e2 = _make(PlanEventType.NODE_COMPLETED, node_id="s1")
        e3 = _make(PlanEventType.NODE_COMPLETED, node_id="s2")

        seq1 = await log.append(e1)
        seq2 = await log.append(e2)
        seq3 = await log.append(e3)

        assert seq1 == 1
        assert seq2 == 2
        assert seq3 == 3
        assert e1.seq == 1
        assert e2.seq == 2
        assert e3.seq == 3

    async def test_seq_is_per_plan_isolated(self, tmp_path):
        log = FileEventLog(tmp_path)
        e1 = _make(PlanEventType.PLAN_CREATED, stream_id="plan-A")
        e2 = _make(PlanEventType.PLAN_CREATED, stream_id="plan-B")

        await log.append(e1)
        await log.append(e2)

        assert e1.seq == 1
        assert e2.seq == 1


class TestEventLogGetAfter:
    async def test_returns_events_after_given_seq(self, tmp_path):
        log = FileEventLog(tmp_path)
        e1 = _make(PlanEventType.PLAN_CREATED)
        e2 = _make(PlanEventType.NODE_COMPLETED, node_id="s1")
        e3 = _make(PlanEventType.NODE_COMPLETED, node_id="s2")
        await log.append(e1)
        await log.append(e2)
        await log.append(e3)

        result = await log.get_after("p1", since_seq=1)
        assert [e.seq for e in result] == [2, 3]

    async def test_regression_same_plan_version_events_not_skipped(self, tmp_path):
        log = FileEventLog(tmp_path)
        created = _make(PlanEventType.PLAN_CREATED, version=1)
        completed1 = _make(PlanEventType.NODE_COMPLETED, version=1, node_id="s1")
        completed2 = _make(PlanEventType.NODE_COMPLETED, version=1, node_id="s2")
        await log.append(created)
        await log.append(completed1)
        await log.append(completed2)

        after = await log.get_after("p1", since_seq=1)
        seqs = [e.seq for e in after]

        assert 2 in seqs, "StepCompleted(s1) must not be lost — same plan version as checkpoint"
        assert 3 in seqs, "StepCompleted(s2) must not be lost — same plan version as checkpoint"

    async def test_empty_when_no_events_after_seq(self, tmp_path):
        log = FileEventLog(tmp_path)
        await log.append(_make(PlanEventType.PLAN_CREATED))
        assert await log.get_after("p1", since_seq=99) == []

    async def test_filters_by_plan_id(self, tmp_path):
        log = FileEventLog(tmp_path)
        await log.append(_make(PlanEventType.PLAN_CREATED, stream_id="A"))
        await log.append(_make(PlanEventType.PLAN_CREATED, stream_id="B"))

        result = await log.get_after("A", since_seq=0)
        assert len(result) == 1
        assert result[0].stream_id == "A"

    async def test_since_seq_zero_returns_all(self, tmp_path):
        log = FileEventLog(tmp_path)
        for i in range(5):
            await log.append(_make(PlanEventType.NODE_COMPLETED, node_id=f"s{i}"))
        assert len(await log.get_after("p1", since_seq=0)) == 5


class TestHybridRestore:
    async def _build_log_with_plan(self, tmp_path) -> tuple[FileEventLog, str]:
        log = FileEventLog(tmp_path)
        plan_id = "plan-1"
        await log.append(
            Event.make(
                PlanEventType.PLAN_CREATED,
                plan_id,
                version=1,
                nodes=[
                    {"node_id": "s1", "status": "pending"},
                    {"node_id": "s2", "status": "pending"},
                ],
            )
        )
        await log.append(Event.make(PlanEventType.NODE_COMPLETED, plan_id, version=1, node_id="s1"))
        await log.append(Event.make(PlanEventType.NODE_COMPLETED, plan_id, version=1, node_id="s2"))
        return log, plan_id

    @pytest.mark.asyncio
    async def test_cold_start_replays_all_events(self, tmp_path):
        log, plan_id = await self._build_log_with_plan(tmp_path)
        cs = FileCheckpointStore(directory=tmp_path / "ckpt")

        state, _, _ = await hybrid_restore(
            plan_id, log, cs, _simple_reducer, extract_base=_extract_base, empty_state=_empty_state
        )

        assert state["nodes"]["s1"]["status"] == "completed"
        assert state["nodes"]["s2"]["status"] == "completed"

    @pytest.mark.asyncio
    async def test_checkpoint_skips_already_applied_events(self, tmp_path):
        log, plan_id = await self._build_log_with_plan(tmp_path)
        cs = FileCheckpointStore(directory=tmp_path / "ckpt")

        run = Run(run_id=plan_id, task="t")
        run.set_cursor(
            "plan",
            {
                "state": {
                    "version": 1,
                    "nodes": {"s1": {"status": "completed"}, "s2": {"status": "completed"}},
                },
                "last_seq": 3,
            },
        )
        await cs.save(run)

        reducer_calls: list[Event] = []

        def counting_reducer(state, event):
            reducer_calls.append(event)
            return _simple_reducer(state, event)

        state, _, _ = await hybrid_restore(
            plan_id, log, cs, counting_reducer, extract_base=_extract_base, empty_state=_empty_state
        )

        assert reducer_calls == [], "no events should be replayed when checkpoint is current"
        assert state["nodes"]["s2"]["status"] == "completed"

    @pytest.mark.asyncio
    async def test_regression_checkpoint_mid_version_replays_missed_events(self, tmp_path):
        log, plan_id = await self._build_log_with_plan(tmp_path)
        cs = FileCheckpointStore(directory=tmp_path / "ckpt")

        run = Run(run_id=plan_id, task="t")
        run.set_cursor(
            "plan",
            {
                "state": {
                    "version": 1,
                    "nodes": {"s1": {"status": "pending"}, "s2": {"status": "pending"}},
                },
                "last_seq": 1,
            },
        )
        await cs.save(run)

        state, _, _ = await hybrid_restore(
            plan_id, log, cs, _simple_reducer, extract_base=_extract_base, empty_state=_empty_state
        )

        assert state["nodes"]["s1"]["status"] == "completed", (
            "StepCompleted(s1) must be replayed — it shares plan version 1 with the checkpoint"
        )
        assert state["nodes"]["s2"]["status"] == "completed", (
            "StepCompleted(s2) must be replayed — it shares plan version 1 with the checkpoint"
        )

    @pytest.mark.asyncio
    async def test_partial_checkpoint_replays_only_tail(self, tmp_path):
        log, plan_id = await self._build_log_with_plan(tmp_path)
        cs = FileCheckpointStore(directory=tmp_path / "ckpt")

        run = Run(run_id=plan_id, task="t")
        run.set_cursor(
            "plan",
            {
                "state": {
                    "version": 1,
                    "nodes": {"s1": {"status": "completed"}, "s2": {"status": "pending"}},
                },
                "last_seq": 2,
            },
        )
        await cs.save(run)

        replayed: list[str] = []

        def tracking_reducer(state, event):
            replayed.append(event.event_type)
            return _simple_reducer(state, event)

        state, _, _ = await hybrid_restore(
            plan_id, log, cs, tracking_reducer, extract_base=_extract_base, empty_state=_empty_state
        )

        assert replayed == [PlanEventType.NODE_COMPLETED], (
            "only s2's StepCompleted should be replayed"
        )
        assert state["nodes"]["s2"]["status"] == "completed"

    @pytest.mark.asyncio
    async def test_multi_plan_isolation(self, tmp_path):
        log = FileEventLog(tmp_path)
        await log.append(
            Event.make(
                PlanEventType.PLAN_CREATED,
                "plan-A",
                version=1,
                nodes=[{"node_id": "a1", "status": "pending"}],
            )
        )
        await log.append(
            Event.make(
                PlanEventType.PLAN_CREATED,
                "plan-B",
                version=1,
                nodes=[{"node_id": "b1", "status": "pending"}],
            )
        )
        await log.append(
            Event.make(PlanEventType.NODE_COMPLETED, "plan-A", version=1, node_id="a1")
        )
        await log.append(
            Event.make(PlanEventType.NODE_COMPLETED, "plan-B", version=1, node_id="b1")
        )

        cs = FileCheckpointStore(directory=tmp_path / "ckpt")

        state_a, _, _ = await hybrid_restore(
            "plan-A", log, cs, _simple_reducer, extract_base=_extract_base, empty_state=_empty_state
        )
        state_b, _, _ = await hybrid_restore(
            "plan-B", log, cs, _simple_reducer, extract_base=_extract_base, empty_state=_empty_state
        )

        assert "a1" in state_a["nodes"]
        assert "b1" not in state_a["nodes"]
        assert "b1" in state_b["nodes"]
        assert "a1" not in state_b["nodes"]


class TestExpectedSeq:
    async def test_matching_expected_seq_appends(self, tmp_path):
        log = FileEventLog(tmp_path)
        await log.append(_make(PlanEventType.PLAN_CREATED))
        seq = await log.append(_make(PlanEventType.NODE_COMPLETED, node_id="s1"), expected_seq=1)
        assert seq == 2

    async def test_stale_expected_seq_raises(self, tmp_path):
        from prodagent import VersionConflict

        log = FileEventLog(tmp_path)
        await log.append(_make(PlanEventType.PLAN_CREATED))
        with pytest.raises(VersionConflict):
            await log.append(_make(PlanEventType.NODE_COMPLETED, node_id="s1"), expected_seq=0)

    async def test_none_expected_seq_skips_check(self, tmp_path):
        log = FileEventLog(tmp_path)
        await log.append(_make(PlanEventType.PLAN_CREATED))
        assert await log.append(_make(PlanEventType.NODE_COMPLETED, node_id="s1")) == 2

    async def test_file_log_detects_concurrent_writer(self, tmp_path):
        from prodagent import VersionConflict

        worker_a = FileEventLog(tmp_path)
        worker_b = FileEventLog(tmp_path)
        await worker_a.append(_make(PlanEventType.PLAN_CREATED))

        await worker_b.append(_make(PlanEventType.NODE_COMPLETED, node_id="s1"), expected_seq=1)

        with pytest.raises(VersionConflict):
            await worker_a.append(_make(PlanEventType.NODE_COMPLETED, node_id="s2"), expected_seq=1)


class TestFileEventLogCrashDurability:
    async def test_half_line_does_not_inflate_disk_seq(self, tmp_path):
        from prodagent.backends.file.event_log import _count_valid_lines

        log = FileEventLog(tmp_path)
        await log.append(_make(PlanEventType.PLAN_CREATED))
        await log.append(_make(PlanEventType.NODE_COMPLETED, node_id="s1"))

        path = log._path("p1")
        with path.open("a", encoding="utf-8") as f:
            f.write('{"seq": 3, "event_type": "NodeCompleted", "data":')

        assert _count_valid_lines(path) == 2

    async def test_half_line_skipped_by_load(self, tmp_path):
        log = FileEventLog(tmp_path)
        await log.append(_make(PlanEventType.PLAN_CREATED))
        await log.append(_make(PlanEventType.NODE_COMPLETED, node_id="s1"))

        path = log._path("p1")
        with path.open("a", encoding="utf-8") as f:
            f.write("this is not valid json at all\n")

        log2 = FileEventLog(tmp_path)
        events = await log2.get_events("p1")
        assert len(events) == 2, "corrupt trailing line must be skipped"
        assert [e.seq for e in events] == [1, 2]

    async def test_append_after_half_line_continues_lsn_correctly(self, tmp_path):
        log = FileEventLog(tmp_path)
        await log.append(_make(PlanEventType.PLAN_CREATED))
        await log.append(_make(PlanEventType.NODE_COMPLETED, node_id="s1"))

        path = log._path("p1")
        with path.open("a", encoding="utf-8") as f:
            f.write('{"seq": 99, "partial":')

        log2 = FileEventLog(tmp_path)
        seq = await log2.append(_make(PlanEventType.NODE_COMPLETED, node_id="s2"))
        assert seq == 3, "LSN must continue from 2 (last valid), not from the corrupt line"

    async def test_fsync_production_mode(self, tmp_path):
        log = FileEventLog(tmp_path, fsync=True)
        assert log._fsync is True

        seq = await log.append(_make(PlanEventType.PLAN_CREATED))
        assert seq == 1

        events = await log.get_events("p1")
        assert len(events) == 1

    async def test_single_write_call_per_append(self, tmp_path):
        import json

        log = FileEventLog(tmp_path)
        await log.append(_make(PlanEventType.PLAN_CREATED))
        await log.append(_make(PlanEventType.NODE_COMPLETED, node_id="s1"))
        await log.append(_make(PlanEventType.NODE_COMPLETED, node_id="s2"))

        path = log._path("p1")
        lines = path.read_text(encoding="utf-8").splitlines()
        assert len(lines) == 3, "expected 3 lines (one per append)"
        for line in lines:
            d = json.loads(line)
            assert "seq" in d
            assert "event_type" in d
            assert "stream_id" in d
