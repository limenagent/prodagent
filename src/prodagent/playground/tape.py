"""The tape-deck API — one run, one tape, every way of playing it.

The UI's single data source is the WAL: a run's markers stream (control
flow), boundary stream (what the outside world answered), and spans stream
(decision snapshots) are the tracks of one tape. This router exposes the
transport:

- ``GET  /events``    — the multi-track snapshot (``since`` per track; the
  suffix law is the reconnect protocol: a client that died at seq N asks
  again with ``since=N`` and misses nothing).
- ``GET  /tail``      — the live transport: one SSE channel merging the
  three tracks via ``subscribe``, play head chasing the recording.
- ``POST /replay``    — re-enact the tape through the replay engine with a
  frozen clock, and return the equivalence verdict against the recorded
  terminal state (the green badge nobody else can show).
- ``GET  /cassette``  — the derived tape as a self-contained JSONL
  artifact (download it, attach it to the ticket, commit it).

The UI is never a source of truth: every endpoint reads; none writes.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
from typing import TYPE_CHECKING, Any

from fastapi import APIRouter
from fastapi.responses import JSONResponse, StreamingResponse

from prodagent.base.event_log import (
    BoundaryEventType,
    RunEventType,
    SpanEventType,
)

if TYPE_CHECKING:
    from prodagent.base.event_log import Event
    from prodagent.ports.observability import EventLog
    from prodagent.ports.persistence import BlobStore

logger = logging.getLogger(__name__)

_HEARTBEAT_S = 15.0
_TERMINAL_GRACE_S = 0.5

_TRACKS: dict[str, tuple[str, Any]] = {
    # track name -> (how to build its stream id, the event types it carries)
    "markers": ("", set(RunEventType)),
    "boundary": ("#boundary", None),
    "spans": ("#spans", {SpanEventType.SPAN_RECORDED}),
}


def _stream_id(run_id: str, suffix: str) -> str:
    return run_id if not suffix else f"{run_id}{suffix}"


def _event_payload(track: str, event: Event) -> dict[str, Any]:
    kind = str(event.event_type)
    if track == "boundary":
        kind = (
            "llm"
            if event.event_type == BoundaryEventType.LLM_RECORDED
            else "tool"
            if event.event_type == BoundaryEventType.TOOL_RECORDED
            else "clock"
        )
    return {
        "track": track,
        "seq": event.seq,
        "type": kind,
        "data": event.data,
    }


def build_tape_router(state: Any, event_log: EventLog, blobs: BlobStore | None) -> APIRouter:
    """Wire the transport onto an app state and its WAL."""
    router = APIRouter(prefix="/api/tape")

    @router.get("/runs")
    async def tape_catalog() -> JSONResponse:
        """The tape catalog, derived from the WAL alone: every run that has
        facts, its child runs (a multi-agent run is a root plus children),
        per-track counts, and the last terminal marker when the tape ended.
        Single-agent and multi-agent runs are the same shape here — streams
        under one root."""
        groups: dict[str, dict[str, Any]] = {}
        for stream_id in await event_log.list_streams():
            root = _root_of(stream_id)
            owner = _owner_of(stream_id)
            group = groups.setdefault(root, {"run_id": root, "lanes": {}, "terminal": None})
            track = _track_of(stream_id)
            events = await event_log.get_after(stream_id, 0)
            group["lanes"][f"{owner}|{track}"] = len(events)
            if track == "markers":
                for event in reversed(events):
                    if str(event.event_type) in {
                        RunEventType.RUN_COMPLETED,
                        RunEventType.RUN_FAILED,
                        RunEventType.RUN_SUSPENDED,
                    }:
                        group["terminal"] = str(event.event_type).split(".")[-1]
                        break
        return JSONResponse({"runs": sorted(groups.values(), key=lambda g: g["run_id"])})

    @router.get("/{run_id}/events")
    async def tape_events(run_id: str, since: int = 0) -> JSONResponse:
        tracks: dict[str, list[dict[str, Any]]] = {}
        for track, (suffix, _types) in _TRACKS.items():
            events = await event_log.get_after(_stream_id(run_id, suffix), since)
            tracks[track] = [_event_payload(track, e) for e in events]
        return JSONResponse({"run_id": run_id, "since": since, "tracks": tracks})

    @router.get("/{run_id}/tail")
    async def tape_tail(run_id: str, since: int = 0) -> StreamingResponse:
        """The live transport: one channel, three subscribed tracks. The
        stream closes shortly after a terminal marker — the tape ended —
        so the UI knows the recording is complete without polling."""

        async def channel() -> Any:
            merged: asyncio.Queue[tuple[str, Event]] = asyncio.Queue()
            terminal_seen = asyncio.Event()

            async def pump(track: str, suffix: str) -> None:
                agen = event_log.subscribe(_stream_id(run_id, suffix), since)
                try:
                    async for event in agen:
                        await merged.put((track, event))
                        if track == "markers" and str(event.event_type) in {
                            RunEventType.RUN_COMPLETED,
                            RunEventType.RUN_FAILED,
                            RunEventType.RUN_SUSPENDED,
                        }:
                            terminal_seen.set()
                finally:
                    closer = getattr(agen, "aclose", None)
                    if closer is not None:
                        with contextlib.suppress(Exception):
                            await closer()

            pumps = [
                asyncio.create_task(pump(track, suffix), name=f"tail-{track}")
                for track, (suffix, _t) in _TRACKS.items()
            ]
            try:
                while True:
                    # Terminal already seen → a short grace wait (the tape is
                    # ending, sibling tracks are flushing); otherwise the long
                    # heartbeat cadence.
                    wait_s = _TERMINAL_GRACE_S if terminal_seen.is_set() else _HEARTBEAT_S
                    try:
                        track, event = await asyncio.wait_for(merged.get(), timeout=wait_s)
                    except TimeoutError:
                        if terminal_seen.is_set():
                            # The tape ended and the grace wait drained —
                            # drain what landed, then stop the transport.
                            while not merged.empty():
                                track, event = merged.get_nowait()
                                yield _sse(_event_payload(track, event))
                            yield "event: tape_end\ndata: {}\n\n"
                            return
                        yield ": heartbeat\n\n"
                        continue
                    yield _sse(_event_payload(track, event))
            finally:
                for pump_task in pumps:
                    pump_task.cancel()
                for pump_task in pumps:
                    with contextlib.suppress(asyncio.CancelledError, Exception):
                        await pump_task

        return StreamingResponse(channel(), media_type="text/event-stream")

    return router


def _sse(payload: dict[str, Any]) -> str:
    return f"data: {json.dumps(payload, default=str, ensure_ascii=False)}\n\n"


async def _recorded_terminal(state: Any, run_id: str) -> dict[str, Any] | None:
    """How the recording ended — from the checkpoint the registry holds."""
    summary = None
    if state.registry is not None:
        summary = await state.registry.load_summary(run_id)
    if summary is None:
        return None
    return {
        "state": summary.state.value,
        "final_output": summary.final_output,
    }


async def _drain_once(queues: dict[str, asyncio.Queue[Event]]) -> tuple[str, Event] | None:
    for track, queue in queues.items():
        if not queue.empty():
            return track, queue.get_nowait()
    return None


async def _any_queue_wait(queues: dict[str, asyncio.Queue[Event]]) -> None:
    """Block until any track produces (or the caller times out)."""
    while _drain_once(queues) is None:
        await asyncio.sleep(0.02)


def override_time(port: Any) -> Any:
    from prodagent.base.determinism import override

    return override(time_port=port)


def _root_of(stream_id: str) -> str:
    """The run a stream belongs to: strip child lineage and track suffix."""
    return stream_id.split("::", 1)[0].split("#", 1)[0]


def _track_of(stream_id: str) -> str:
    if "#" in stream_id:
        return stream_id.rsplit("#", 1)[1]
    return "markers"


def _owner_of(stream_id: str) -> str:
    """Whose lane it is: the root, or a named child (``root::child``)."""
    return stream_id.split("#", 1)[0]
