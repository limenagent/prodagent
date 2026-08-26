from __future__ import annotations

import pytest

from prodagent.base.errors import InfiniteLoopDetected
from prodagent.kernel.progress import ProgressMonitor, _tool_fingerprint
from prodagent.kernel.state import AgentRun
from prodagent.kernel.types import ToolCall


def _call(name: str, **params) -> ToolCall:
    return ToolCall(name=name, params=params, call_id="c")


@pytest.fixture
def run():
    return AgentRun(run_id="test", task="t")


def _monitor(**kw):
    return ProgressMonitor(
        repeat_threshold=kw.get("repeat_threshold", 5),
        window_size=kw.get("window_size", 5),
        stall_threshold=kw.get("stall_threshold", 99),
    )


class TestDeadLoopDetection:
    def test_six_identical_calls_trip_in_window_five(self, run):
        call = _call("read_tool_result", path="f.json", offset=0, limit=100)
        mon = _monitor()
        for _ in range(4):
            mon.check(run, new_call=call)
        with pytest.raises(InfiniteLoopDetected):
            mon.check(run, new_call=call)

    def test_advancing_offset_does_not_trip(self, run):
        mon = _monitor()
        for offset in (0, 130, 4990, 6790, 7540, 8000):
            call = _call("read_tool_result", path="f.json", offset=offset, limit=100)
            mon.check(run, new_call=call)

    def test_varying_limit_at_fixed_offset_trips(self, run):
        # Re-reading the same head (path + offset=0) with different row counts
        # is the dead-loop pattern: the LLM keeps re-fetching the start of the
        # same file instead of advancing the offset. `limit` is stripped from
        # the fingerprint precisely so this is detected.
        mon = _monitor()
        for limit in (10, 50, 100, 200):
            call = _call("read_tool_result", path="f.json", offset=0, limit=limit)
            mon.check(run, new_call=call)
        with pytest.raises(InfiniteLoopDetected):
            call = _call("read_tool_result", path="f.json", offset=0, limit=500)
            mon.check(run, new_call=call)

    def test_different_paths_are_distinct(self, run):
        mon = _monitor()
        for path in ("a.json", "b.json", "c.json", "d.json", "e.json", "f.json"):
            call = _call("read_tool_result", path=path, offset=0, limit=100)
            mon.check(run, new_call=call)

    def test_below_threshold_no_trip(self, run):
        call = _call("grep", pattern="error")
        mon = _monitor()
        for _ in range(4):
            mon.check(run, new_call=call)

    def test_mixed_then_repeat_does_not_carry_stale_count(self, run):
        a = _call("search", query="x")
        b = _call("search", query="y")
        mon = _monitor()
        for _ in range(3):
            mon.check(run, new_call=a)
            mon.check(run, new_call=b)
        assert len(run.fingerprints) <= 5

    def test_window_slides_old_burst_stops_counting(self, run):
        a = _call("grep", pattern="error")
        mon = _monitor()
        for _ in range(4):
            mon.check(run, new_call=a)
        for i in range(6):
            distinct = _call("grep", pattern=f"p{i}")
            mon.check(run, new_call=distinct)
        a_key = _tool_fingerprint(a)
        assert a_key not in run.fingerprints
        assert len(run.fingerprints) == 5
        # The old burst was evicted: one more identical call must not trip.
        mon.check(run, new_call=a)

    def test_window_scroll_evicts_oldest_fingerprint(self, run):
        a = _call("grep", pattern="error")
        mon = _monitor()
        for _ in range(4):
            mon.check(run, new_call=a)
        with pytest.raises(InfiniteLoopDetected):
            mon.check(run, new_call=a)
        a_key = _tool_fingerprint(a)
        assert run.fingerprints == [a_key] * 5

    def test_fingerprint_window_survives_serialization(self, run):
        a = _call("grep", pattern="error")
        mon = _monitor()
        for _ in range(4):
            mon.check(run, new_call=a)
        restored = AgentRun.from_dict(run.to_dict())
        a_key = _tool_fingerprint(a)
        assert restored.fingerprints == [a_key] * 4
        # A resumed run keeps its loop memory: the 5th identical call trips.
        with pytest.raises(InfiniteLoopDetected):
            mon.check(restored, new_call=a)
