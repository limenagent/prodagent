"""Multi-agent UI envelope and adapter protocol tests.

Covers the generic :class:`MultiAgentEvent` / :class:`MultiAgentAdapter`
machinery in :mod:`prodagent.playground.multiagent` and the example adapters
that plug into it. Does not exercise the FastAPI SSE route end-to-end —
``test_run_registry`` already does that for the single-agent path; the
multi-agent route is thin glue on top of :class:`MultiAgentRun`.
"""

from __future__ import annotations

import asyncio
import os
from typing import Any

import pytest

from prodagent.playground.multiagent import (
    MultiAgentEvent,
    MultiAgentRun,
    ParticipantStatus,
    PhaseCompleted,
    PhaseStarted,
    event_to_dict,
)

# ---------------------------------------------------------------------------
# Envelope — JSON shape and participant serialization
# ---------------------------------------------------------------------------


def test_event_to_dict_round_trips_envelope_fields() -> None:
    env = MultiAgentEvent(
        kind="turn",
        actor="alice",
        phase="live",
        summary={"verb": "spoke", "object": "hi"},
        payload={"text": "hi"},
        snapshot={"turn_count": 1},
    )
    d = event_to_dict(env)
    assert d["type"] == "event"
    assert d["kind"] == "turn"
    assert d["actor"] == "alice"
    assert d["phase"] == "live"
    assert d["summary"] == {"verb": "spoke", "object": "hi"}
    assert d["payload"] == {"text": "hi"}
    assert d["snapshot"] == {"turn_count": 1}
    assert isinstance(d["timestamp"], float)


def test_event_to_dict_coerces_collections() -> None:
    """The envelope's payload may contain set/frozenset values — _jsonable
    must coerce them to lists so the SSE stream stays JSON-serializable."""

    env = MultiAgentEvent(
        kind="write",
        actor="bob",
        phase=None,
        summary={},
        payload={"frozenset": frozenset({"a", "b"}), "tuple": (1, 2, 3)},
        snapshot={},
    )
    d = event_to_dict(env)
    assert sorted(d["payload"]["frozenset"]) == ["a", "b"]
    assert d["payload"]["tuple"] == [1, 2, 3]


# ---------------------------------------------------------------------------
# MultiAgentRun — driver pumps adapter events through map_event onto the queue
# ---------------------------------------------------------------------------


class _StubAdapter:
    """Minimal adapter that yields a fixed sequence: roster + two phase markers
    + a turn, then completes. Used to exercise the driver without pulling in
    example deps."""

    name = "stub"

    def __init__(self) -> None:
        self._attached_run: str | None = None

    def initial_participants(self) -> list[ParticipantStatus]:
        return [ParticipantStatus(name="alice", role="speaker", state="idle", meta={})]

    def map_event(self, event: Any) -> MultiAgentEvent | list[MultiAgentEvent]:
        if isinstance(event, PhaseStarted):
            return MultiAgentEvent(
                kind="phase_started",
                actor=None,
                phase=event.phase,
                summary={"verb": "phase_started", "object": event.phase},
                payload={},
                snapshot={},
            )
        if isinstance(event, PhaseCompleted):
            return MultiAgentEvent(
                kind="phase_completed",
                actor=None,
                phase=event.phase,
                summary={"verb": "phase_completed", "object": event.phase},
                payload={},
                snapshot={},
            )
        if isinstance(event, str):
            return MultiAgentEvent(
                kind="turn",
                actor=event,
                phase=None,
                summary={"verb": "spoke", "object": event},
                payload={"text": event},
                snapshot={},
            )
        raise TypeError(f"unknown event {event!r}")

    async def stream(self):
        yield PhaseStarted("phase1")
        yield "hello"
        yield PhaseCompleted("phase1", counts={"turns": 1})


def test_multi_agent_run_seeds_roster_then_pumps_events() -> None:
    """The driver emits a ``roster`` event first, then ``started``, then
    adapter-mapped events, then ``completed``. Terminal events mark the run."""

    async def _run() -> list[dict[str, Any]]:
        adapter = _StubAdapter()
        run = MultiAgentRun(adapter, run_id="stub-run")
        run.start()
        events: list[dict[str, Any]] = []
        while True:
            ev = await run.queue.get()
            events.append(ev)
            if ev.get("kind") in ("completed", "failed"):
                return events

    events = asyncio.run(_run())
    kinds = [e["kind"] for e in events]
    assert kinds[0] == "roster"
    assert kinds[1] == "started"
    assert kinds[2] == "phase_started"
    assert kinds[3] == "turn"
    assert kinds[4] == "phase_completed"
    assert kinds[-1] == "completed"
    assert events[0]["payload"]["participants"][0]["name"] == "alice"


def test_adapter_cannot_attach_to_two_runs() -> None:
    adapter = _StubAdapter()
    MultiAgentRun(adapter, run_id="run-1")
    with pytest.raises(RuntimeError, match="already attached"):
        MultiAgentRun(adapter, run_id="run-2")


def test_driver_emits_failed_envelope_on_adapter_crash() -> None:
    class _CrashingAdapter(_StubAdapter):
        async def stream(self):
            yield PhaseStarted("phase1")
            raise RuntimeError("kaboom")

    async def _run() -> dict[str, Any]:
        adapter = _CrashingAdapter()
        run = MultiAgentRun(adapter, run_id="crash-run")
        run.start()
        while True:
            ev = await run.queue.get()
            if ev.get("kind") in ("completed", "failed"):
                return ev

    ev = asyncio.run(_run())
    assert ev["kind"] == "failed"
    assert "kaboom" in ev["payload"]["error"]


# ---------------------------------------------------------------------------
# Example adapter: dating_chat — yields kind="turn" events from run_conversation
# ---------------------------------------------------------------------------


def test_dating_chat_adapter_yields_turn_events() -> None:
    """The dating_chat adapter should yield ``kind="turn"`` events whose payload
    carries the Line fields (speaker, text, round, memory_hits, …). Run under
    FakeLLM so the test is hermetic."""

    os.environ["USE_FAKE_LLM"] = "1"
    try:
        from prodagent.playground.registry import discover_examples

        specs = discover_examples()
        spec = next((s for s in specs if s.name == "dating_chat"), None)
        assert spec is not None, "dating_chat not discovered"
        assert spec.multiagent_adapter is not None, "dating_chat has no adapter"
        build_adapter = spec.multiagent_adapter
    finally:
        os.environ.pop("USE_FAKE_LLM", None)

    async def _run() -> list[dict[str, Any]]:
        adapter = build_adapter()
        run = MultiAgentRun(adapter, run_id="dating-test")
        run.start()
        events: list[dict[str, Any]] = []
        while True:
            ev = await run.queue.get()
            events.append(ev)
            if ev.get("kind") in ("completed", "failed"):
                return events

    events = asyncio.run(_run())
    turns = [e for e in events if e["kind"] == "turn"]
    assert len(turns) >= 2, "dating_chat should produce at least two turns"
    for ev in turns:
        assert ev["actor"] in ("大牛", "小美")
        assert "text" in ev["payload"]
        assert "round" in ev["payload"]
    assert events[0]["kind"] == "roster"
    assert events[-1]["kind"] == "completed"


# ---------------------------------------------------------------------------
# Example adapter: quiz_arena — two phases, WorkQueue then Blackboard
# ---------------------------------------------------------------------------


def test_quiz_arena_adapter_streams_two_phases() -> None:
    """The quiz_arena adapter must emit phase_started/phase_completed markers
    for both ``backstage_review`` and ``live_quiz`` phases, with WorkQueue
    events (claim/complete/requeue/dead_letter) in phase 1 and Blackboard
    writes (host writes state, contestant buzz_in winners write answer) in
    phase 2. Run under FakeLLM so contestants get the embedded hint echo."""

    os.environ["USE_FAKE_LLM"] = "1"
    try:
        from prodagent.playground.registry import discover_examples

        specs = discover_examples()
        spec = next((s for s in specs if s.name == "quiz_arena"), None)
        assert spec is not None, "quiz_arena not discovered"
        assert spec.multiagent_adapter is not None, "quiz_arena has no adapter"
        build_adapter = spec.multiagent_adapter
    finally:
        os.environ.pop("USE_FAKE_LLM", None)

    async def _run() -> list[dict[str, Any]]:
        adapter = build_adapter()
        run = MultiAgentRun(adapter, run_id="quiz-test")
        run.start()
        events: list[dict[str, Any]] = []
        while True:
            ev = await run.queue.get()
            events.append(ev)
            if ev.get("kind") in ("completed", "failed"):
                return events

    events = asyncio.run(_run())

    # Phase markers
    phase_starts = [e for e in events if e["kind"] == "phase_started"]
    phase_completes = [e for e in events if e["kind"] == "phase_completed"]
    phases_seen = {e["phase"] for e in phase_starts}
    assert phases_seen == {"backstage_review", "live_quiz"}, phases_seen
    assert len(phase_completes) >= 2  # at least one per phase + queue_drained

    # Phase 1 events should all carry phase=backstage_review
    phase1_kinds = {e["kind"] for e in events if e["phase"] == "backstage_review"}
    assert "claim" in phase1_kinds
    assert "complete" in phase1_kinds
    # q4/q5 are unvalidatable — they must end up dead-lettered
    assert "dead_letter" in phase1_kinds

    # Phase 2 events should all carry phase=live_quiz
    phase2_writes = [e for e in events if e["kind"] == "write" and e["phase"] == "live_quiz"]
    assert len(phase2_writes) >= 4, "expected at least host-state + answer writes"
    # The final event is completion
    assert events[-1]["kind"] == "completed"
    assert events[-1]["payload"]["final_state"].get("finished") is True


def test_discover_examples_finds_quiz_arena_without_agent_py() -> None:
    """quiz_arena has only ``multiagent.py`` (no ``agent.py``) — it must still
    be discoverable, with ``factory=None`` and ``is_multiagent=True``."""

    from prodagent.playground.registry import discover_examples

    specs = discover_examples()
    quiz = next((s for s in specs if s.name == "quiz_arena"), None)
    assert quiz is not None, "quiz_arena should be discovered"
    assert quiz.factory is None
    assert quiz.is_multiagent is True
    assert "is_multiagent" in quiz.to_dict()
    assert quiz.to_dict()["is_multiagent"] is True


def test_single_agent_only_example_is_not_multiagent() -> None:
    """A single-agent example like ``greeter`` must have ``factory`` set and
    ``is_multiagent=False`` — the frontend uses this flag to pick UI mode."""

    from prodagent.playground.registry import discover_examples

    specs = discover_examples()
    greeter = next((s for s in specs if s.name == "greeter"), None)
    assert greeter is not None
    assert greeter.factory is not None
    assert greeter.is_multiagent is False
    assert greeter.to_dict()["is_multiagent"] is False
