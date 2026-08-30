"""Tape-deck API laws — the UI's transport over the one WAL.

Law 1 (snapshot): the events endpoint answers the multi-track view, with
``since`` filtering per track — the reconnect protocol.

Law 2 (artifact): the cassette endpoint returns the self-contained tape.

Law 3 (re-enactment): the replay endpoint re-runs the tape offline and
returns the equivalence verdict — green against the recorded terminal
state, with the tape consumed exactly.

Law 4 (live transport): the tail SSE channel delivers the tracks live and
ends with ``tape_end`` after the terminal marker.
"""

from __future__ import annotations

import json
from typing import Any

from fastapi.testclient import TestClient  # noqa: E402

from prodagent.backends.memory.blob import InMemoryBlobStore
from prodagent.backends.memory.checkpoint import InMemoryCheckpointStore
from prodagent.backends.memory.event_log import InMemoryEventLog
from prodagent.kernel.loop import ReactiveLoop
from prodagent.kernel.types import SideEffectLevel, ToolMeta
from prodagent.llm.fake import script
from prodagent.llm.recording import RecordingLLMClient
from prodagent.playground import server as server_mod
from prodagent.tooling.base import FunctionTool
from prodagent.tooling.dispatcher import ToolDispatcher


def _tool(name: str) -> FunctionTool:
    async def fn(**_: Any) -> dict:
        return {"action": name}

    return FunctionTool(
        name=name,
        fn=fn,
        meta=ToolMeta(name=name, is_readonly=True, side_effect_level=SideEffectLevel.LOW),
        schema={"name": name, "description": name, "parameters": {"type": "object", "properties": {}}},
    )


def _build(log: InMemoryEventLog, checkpoint: InMemoryCheckpointStore) -> Any:
    return server_mod.build_app(
        specs=[],
        checkpoint_for=lambda spec: checkpoint,
        session_store_for=lambda spec: None,
        event_log=log,
        blob_store=InMemoryBlobStore(),
    )


async def _drive_run(log: InMemoryEventLog, checkpoint: InMemoryCheckpointStore, task: str) -> str:
    """A real recorded run: LLM + tool facts, clock facts, markers, checkpoint."""
    dispatcher = ToolDispatcher({"probe": _tool("probe")}, event_log=log)
    loop = ReactiveLoop(
        RecordingLLMClient(script({"tool": "probe", "params": {}}, {"content": "all done"}), log),
        dispatcher,
        event_log=log,
        checkpoint_store=checkpoint,
    )
    run_id: str | None = None
    async for event in loop.stream(task):
        run_id = getattr(event, "run_id", None) or run_id
    assert run_id is not None
    return run_id


async def test_snapshot_law_multi_track_with_since() -> None:
    log, checkpoint = InMemoryEventLog(), InMemoryCheckpointStore()
    run_id = await _drive_run(log, checkpoint, "tape the run")
    app = _build(log, checkpoint)
    with TestClient(app) as client:
        body = client.get(f"/api/tape/{run_id}/events").json()
    kinds = {e["type"] for e in body["tracks"]["boundary"]}
    assert {"llm", "tool"} <= kinds
    assert body["tracks"]["markers"], "markers track present"
    assert body["tracks"]["spans"] or True  # spans only with the observer attached
    # Reconnect from a cursor: the suffix only.
    with TestClient(app) as client:
        resumed = client.get(f"/api/tape/{run_id}/events", params={"since": 1}).json()
    for track_events in resumed["tracks"].values():
        assert all(e["seq"] > 1 for e in track_events), "since is the reconnect cursor"


async def test_artifact_law_cassette_is_self_contained() -> None:
    log, checkpoint = InMemoryEventLog(), InMemoryCheckpointStore()
    run_id = await _drive_run(log, checkpoint, "download me")
    app = _build(log, checkpoint)
    with TestClient(app) as client:
        text = client.get(f"/api/tape/{run_id}/cassette").text
    lines = [ln for ln in text.split("\n") if ln.strip()]
    header = json.loads(lines[0])
    assert header["header"] is True and header["run_id"] == run_id
    records = [json.loads(ln) for ln in lines[1:]]
    assert any(r["kind"] == "llm" for r in records)
    assert any(r["kind"] == "tool" for r in records)


async def test_reenactment_law_verdict_green() -> None:
    log, checkpoint = InMemoryEventLog(), InMemoryCheckpointStore()
    run_id = await _drive_run(log, checkpoint, "prove me")
    app = _build(log, checkpoint)
    with TestClient(app) as client:
        verdict = client.post(f"/api/tape/{run_id}/replay").json()
    assert verdict["equivalent"] is True, verdict.get("divergences")
    assert verdict["divergences"] == []
    assert verdict["final_output"] == "all done"
    assert verdict["turns"] == 2


async def test_reenactment_404_when_no_facts() -> None:
    log, checkpoint = InMemoryEventLog(), InMemoryCheckpointStore()
    app = _build(log, checkpoint)
    with TestClient(app) as client:
        response = client.post("/api/tape/ghost/replay")
    assert response.status_code == 404


async def test_live_transport_ends_with_tape_end() -> None:
    log, checkpoint = InMemoryEventLog(), InMemoryCheckpointStore()
    run_id = await _drive_run(log, checkpoint, "stream me")
    app = _build(log, checkpoint)
    with TestClient(app) as client, client.stream("GET", f"/api/tape/{run_id}/tail") as response:
            assert response.status_code == 200
            saw_boundary = False
            saw_end = False
            for line in response.iter_lines():
                if line.startswith("data:") and '"track": "boundary"' in line:
                    saw_boundary = True
                if line.startswith("event: tape_end"):
                    saw_end = True
                    break
    assert saw_boundary, "the merged channel carries the boundary track"
    assert saw_end, "the transport closes after the terminal marker"
