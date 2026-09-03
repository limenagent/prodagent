"""Event-sourcing laws — the invariants that make replay trustworthy.

Two laws, both machine-checked over arbitrary event sequences:

1. **Stream order law** (EventLog port, parametric over backends): append
   assigns consecutive seqs starting at 1, ``get_events`` returns append
   order, and ``get_after(k)`` returns exactly the suffix ``seq > k``.
   Every recovery path builds on this; a backend that violates it silently
   double-applies or drops transitions on resume.

2. **Prefix-replay law** (the reducer's side): folding all events equals
   folding a JSON-serialized prefix state, then folding the remaining tail —
   which is exactly what ``hybrid_restore`` does (checkpoint-as-base + exact
   tail replay). If this law broke, checkpointing itself would be unsound:
   resuming from a snapshot would land on a different state than a full
   replay.
"""

from __future__ import annotations

import asyncio
import json
import tempfile
from pathlib import Path
from typing import Any

from hypothesis import given, settings
from hypothesis import strategies as st

from prodagent.base.event_log import Event
from prodagent.kernel.event_log import apply_event  # the real plan reducer

_node_ids = st.sampled_from(["s1", "s2", "s3", "s4"])
_texts = st.text(min_size=0, max_size=24)
_kinds = st.sampled_from(
    ["PLAN_CREATED", "NODE_STARTED", "NODE_COMPLETED", "NODE_FAILED", "NODE_SUSPENDED"]
)


@st.composite
def _plan_events(draw: Any) -> list[Event]:
    events: list[Event] = []
    for _ in range(draw(st.integers(min_value=0, max_value=15))):
        kind = draw(_kinds)
        sid = draw(_node_ids)
        if kind == "PLAN_CREATED":
            steps = draw(st.lists(_node_ids, min_size=0, max_size=4))
            data: dict[str, Any] = {
                "nodes": [{"node_id": s, "status": "pending"} for s in dict.fromkeys(steps)]
            }
        elif kind == "NODE_COMPLETED":
            data = {"node_id": sid, "output_ref": {"result": draw(_texts)}}
        elif kind == "NODE_FAILED":
            data = {"node_id": sid, "error": draw(_texts)}
        else:
            data = {"node_id": sid}
        events.append(Event.make(kind, stream_id="p1", version=1, **data))
    return events


# ── Law 1: stream order, parametric over backends ─────────────────────────────


async def _assert_stream_order_law(log: Any, events: list[Event]) -> None:
    seqs = [await log.append(e) for e in events]
    assert seqs == list(range(1, len(events) + 1)), "append must assign 1..N consecutively"
    stored = await log.get_events("p1")
    assert [e.seq for e in stored] == seqs
    assert [e.event_id for e in stored] == [e.event_id for e in events], "append order preserved"
    k = len(events) // 2
    tail = await log.get_after("p1", since_seq=k)
    assert [e.seq for e in tail] == [s for s in seqs if s > k], "get_after is exactly the suffix"


_settings = settings(max_examples=50, deadline=None)


@_settings
@given(_plan_events())
def test_stream_order_law_memory(events: list[Event]) -> None:
    from prodagent.backends.factory import in_memory_event_log

    asyncio.run(_assert_stream_order_law(in_memory_event_log(), events))


@_settings
@given(_plan_events())
def test_stream_order_law_file(events: list[Event]) -> None:
    from prodagent.backends.file.event_log import FileEventLog

    with tempfile.TemporaryDirectory() as tmp:
        asyncio.run(_assert_stream_order_law(FileEventLog(Path(tmp)), events))


# ── Law 2: prefix replay ≡ full replay, through JSON snapshots ────────────────


def _fold(state: dict[str, Any], events: list[Event]) -> dict[str, Any]:
    for e in events:
        apply_event(state, e)
    return state


@_settings
@given(_plan_events())
def test_prefix_replay_equivalence_law(events: list[Event]) -> None:
    """checkpoint(prefix) + tail ≡ full replay — the soundness of resume.

    The prefix state is round-tripped through JSON before continuing, because
    that is what a checkpoint actually stores. Every split point is checked."""
    full = _fold({"nodes": {}, "version": 0}, events)
    for k in range(len(events) + 1):
        prefix = _fold({"nodes": {}, "version": 0}, events[:k])
        via_checkpoint = json.loads(json.dumps(prefix))  # the durable form
        resumed = _fold(via_checkpoint, events[k:])
        assert resumed == full, f"resume from prefix of {k} events diverges from full replay"
