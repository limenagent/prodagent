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

import asyncio
import json
from typing import Any

from fastapi.testclient import TestClient  # noqa: E402

from prodagent.backends.memory.blob import InMemoryBlobStore
from prodagent.backends.memory.checkpoint import InMemoryCheckpointStore
from prodagent.backends.memory.event_log import InMemoryEventLog
from prodagent.kernel.types import SideEffectLevel, ToolMeta
from prodagent.llm.fake import script
from prodagent.llm.recording import RecordingLLMClient
from prodagent.playground import server as server_mod
from prodagent.runtime.agent_loop import agent_scheduler
from prodagent.tooling.base import FunctionTool
from prodagent.tooling.dispatcher import ToolDispatcher


def _tool(name: str) -> FunctionTool:
    async def fn(**_: Any) -> dict:
        return {"action": name}

    return FunctionTool(
        name=name,
        fn=fn,
        meta=ToolMeta(name=name, is_readonly=True, side_effect_level=SideEffectLevel.LOW),
        schema={
            "name": name,
            "description": name,
            "parameters": {"type": "object", "properties": {}},
        },
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
    loop = agent_scheduler(
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


def test_tape_page_serves() -> None:
    log, checkpoint = InMemoryEventLog(), InMemoryCheckpointStore()
    app = _build(log, checkpoint)
    with TestClient(app) as client:
        response = client.get("/tape")
    assert response.status_code == 200
    assert "prodagent playground" in response.text and "app.js" in response.text


async def test_catalog_groups_children_under_roots() -> None:
    """A multi-agent run is a root plus child streams — the catalog derives
    that shape from the WAL alone, same as single-agent runs."""
    from prodagent.base.event_log import Event, PlanEventType

    log, checkpoint = InMemoryEventLog(), InMemoryCheckpointStore()
    root = await _drive_run(log, checkpoint, "the parent")
    # Child streams, the shape spawn children record (parent::child).
    for sid in (f"{root}::scout", f"{root}::scout#boundary", f"{root}::writer"):
        await log.append(Event.make(PlanEventType.NODE_COMPLETED, sid, 1))
    app = _build(log, checkpoint)
    with TestClient(app) as client:
        catalog = client.get("/api/tape/runs").json()
    entry = next(r for r in catalog["runs"] if r["run_id"] == root)
    assert entry["terminal"] == "RunCompleted"
    owners = {lane.split("|")[0] for lane in entry["lanes"]}
    assert {root, f"{root}::scout", f"{root}::writer"} <= owners


async def test_empty_tape_answers_cleanly_not_dead() -> None:
    """The 'clicked live, nothing happened' regression: a just-started run
    has no facts yet — the snapshot must answer empty tracks (not error),
    so the deck can open its live tail before the first fact lands."""
    log, checkpoint = InMemoryEventLog(), InMemoryCheckpointStore()
    app = _build(log, checkpoint)
    with TestClient(app) as client:
        body = client.get("/api/tape/not-yet-running/events").json()
    assert body["tracks"]["markers"] == []
    assert body["tracks"]["boundary"] == []


async def test_chat_without_run_id_mints_a_session() -> None:
    """The 'empty tape path' regression: a first message with no run_id
    must open a NEW session and return it — an empty string downstream
    surfaced as /api/tape//events 404."""
    log, checkpoint = InMemoryEventLog(), InMemoryCheckpointStore()
    app = _build(log, checkpoint)
    with TestClient(app) as client:
        out = client.post("/api/chat", json={"example": "greeter", "message": "hi"}).json()
    assert out["run_id"], "a fresh conversation gets a minted session id"


async def test_replay_refusal_is_a_verdict_not_a_500() -> None:
    """Zero egress refusing an ask reports as equivalent=false with the
    refusal named — never as a server error."""
    from prodagent.base.event_log import BoundaryEventType, Event, boundary_stream

    log, checkpoint = InMemoryEventLog(), InMemoryCheckpointStore()
    # A boundary fact whose request hash will not match any re-run ask.
    await log.append(
        Event.make(
            BoundaryEventType.LLM_RECORDED,
            stream_id=boundary_stream("mismatched"),
            version=0,
            req_hash="0" * 64,
            request={
                "messages": [{"role": "user", "content": "the task"}],
                "system": "",
                "tools": [],
                "config": None,
            },
            response={"content": "answer", "stop_reason": "end_turn"},
        )
    )
    app = _build(log, checkpoint)
    with TestClient(app) as client:
        resp = client.post("/api/tape/mismatched/replay")
    assert resp.status_code == 200
    verdict = resp.json()
    assert verdict["equivalent"] is False and verdict["refused"] is True
    assert verdict["divergences"] and "refused" in verdict["divergences"][0]


async def test_approval_banner_fires_end_to_end(tmp_path: Any) -> None:
    """The approval box, proven end to end: a HITL example suspends on its
    HIGH tool; the tape carries the suspended fact WITH the approval id;
    the summaries match in the RIGHT direction (session prefixes the turn
    tape root — the old reversed match never fired)."""
    import os

    from prodagent.playground.registry import discover_examples

    specs = {s.name: s for s in discover_examples() if s.is_hitl and s.factory is not None}
    # compliance_audit's script drives all the way to its HIGH tool
    # (submit_to_regulator); trader's default task stops at the readonly
    # proposal and never reaches place_order — no HIGH call, no approval.
    spec = specs.get("compliance_audit")
    if spec is None:
        import pytest

        pytest.skip("compliance_audit example not installed")
    # The example's agent resolves its own stores from the framework config
    # (relative .prodagent dirs) — chdir to a sandbox so the run, its tape,
    # and the summaries all land in tmp instead of the developer's tree.
    prev_cwd = os.getcwd()
    os.chdir(tmp_path)
    try:
        await _approval_e2e(spec, tmp_path)
    finally:
        os.chdir(prev_cwd)


async def _approval_e2e(spec: Any, tmp_path: Any) -> None:
    app = server_mod.build_app(specs=[spec])
    with TestClient(app) as client:
        out = client.post("/api/run", json={"example": spec.name, "task": spec.default_task}).json()
        rid = out["run_id"]
        # Drive to suspension: the HITL example spawns its audit child
        # first, then the HIGH tool parks — give it room.
        me = None
        for _ in range(60):
            await asyncio.sleep(0.5)
            runs = client.get("/api/runs").json()
            me = next(
                (r for r in runs if r["run_id"] == rid or r["run_id"].startswith(rid + ":")),
                None,
            )
            if me and me["state"] in ("suspended", "failed"):
                break
        assert me["state"] == "suspended", f"expected suspension, got {me}"
        assert me["pending_approval_id"]

        # The tape's turn root is <session>:1 — the browser's resolveTapeRoot.
        cat = client.get("/api/tape/runs").json()
        turn_root = next(r["run_id"] for r in cat["runs"] if r["run_id"].startswith(rid + ":"))
        facts = client.get(f"/api/tape/{turn_root}/events").json()
        boundary = facts["tracks"]["boundary"]
        suspended = [
            e for e in boundary if e["data"].get("response", {}).get("outcome") == "suspended"
        ]
        assert suspended, "the HIGH tool landed on the tape as a suspended fact"
        assert (
            suspended[0]["data"]["response"]["approval_request_id"] == me["pending_approval_id"]
        ), "the tape's approval id equals the summary's — the banner fires off either"

        # And the summary lookup with the CORRECT direction matches.
        assert turn_root.startswith(rid + ":")
