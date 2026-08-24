"""FileSessionStore — atomic save/load with optimistic versioning.

Covers the persistence contract the chat path relies on: load-then-create,
version bumps on save, version-conflict detection, and round-trip
preservation of messages and turns.
"""

from __future__ import annotations

import pytest

from prodagent.backends.file.session_store import FileSessionStore
from prodagent.core.state.session import ConversationSession
from prodagent.kernel.state import AgentRun
from prodagent.kernel.types import ExecutionMode, RunState


def _completed_run(run_id: str, messages=None, final_output: str = "ok") -> AgentRun:
    run = AgentRun(run_id=run_id, task="t")
    run.state = RunState.COMPLETED
    run.final_output = final_output
    if messages:
        run.messages = list(messages)
    return run


async def _load_or_create(
    store: FileSessionStore, session_id: str, agent_id: str
) -> ConversationSession:
    s = await store.load(session_id)
    if s is None:
        s = ConversationSession(session_id=session_id, agent_id=agent_id)
    return s


@pytest.mark.asyncio
async def test_load_returns_none_for_unknown_session(tmp_path):
    store = FileSessionStore(tmp_path / "sessions")
    assert await store.load("nope") is None


@pytest.mark.asyncio
async def test_load_then_create_preserves_messages_on_reload(tmp_path):
    store = FileSessionStore(tmp_path / "sessions")
    s1 = await _load_or_create(store, "sess-A", agent_id="agent")
    assert s1.session_id == "sess-A"
    assert s1.agent_id == "agent"
    assert s1.messages == []

    rid = s1.start_turn("hi", mode=ExecutionMode.REACTIVE).run_id
    s1.complete_turn(
        rid,
        ExecutionMode.REACTIVE,
        _completed_run(
            rid,
            messages=[{"role": "user", "content": "hi"}, {"role": "assistant", "content": "hey"}],
        ),
    )
    await store.save(s1, expected_version=0)

    s2 = await store.load("sess-A")
    assert s2 is not None
    assert s2.session_id == "sess-A"
    assert len(s2.messages) == 2
    assert s2.messages[1]["content"] == "hey"
    assert len(s2.turns) == 1
    assert s2.turns[0].state is RunState.COMPLETED


@pytest.mark.asyncio
async def test_save_bumps_version_each_write(tmp_path):
    store = FileSessionStore(tmp_path / "sessions")
    s = await _load_or_create(store, "sess-A", agent_id="agent")
    assert s.version == 0

    rid = s.start_turn("m1", mode=ExecutionMode.REACTIVE).run_id
    s.complete_turn(rid, ExecutionMode.REACTIVE, _completed_run(rid))
    await store.save(s, expected_version=0)
    assert s.version == 1

    s2 = await store.load("sess-A")
    assert s2 is not None
    assert s2.version == 1


@pytest.mark.asyncio
async def test_save_rejects_stale_expected_version(tmp_path):
    from prodagent.core.exceptions import VersionConflict

    store = FileSessionStore(tmp_path / "sessions")
    s = await _load_or_create(store, "sess-A", agent_id="agent")
    rid = s.start_turn("m1", mode=ExecutionMode.REACTIVE).run_id
    s.complete_turn(rid, ExecutionMode.REACTIVE, _completed_run(rid))
    await store.save(s, expected_version=0)
    assert s.version == 1

    # A concurrent writer saved v2; the stale local copy with v0 must fail.
    s2 = await store.load("sess-A")
    assert s2.version == 1
    rid2 = s2.start_turn("m2", mode=ExecutionMode.REACTIVE).run_id
    s2.complete_turn(rid2, ExecutionMode.REACTIVE, _completed_run(rid2, final_output="r2"))
    await store.save(s2, expected_version=1)
    assert s2.version == 2

    with pytest.raises(VersionConflict):
        await store.save(s, expected_version=0)


@pytest.mark.asyncio
async def test_save_with_expected_version_none_skips_check(tmp_path):
    store = FileSessionStore(tmp_path / "sessions")
    s = await _load_or_create(store, "sess-A", agent_id="agent")
    rid = s.start_turn("m", mode=ExecutionMode.REACTIVE).run_id
    s.complete_turn(rid, ExecutionMode.REACTIVE, _completed_run(rid))
    # No expected_version — unconditional write.
    await store.save(s)
    assert s.version == 1

    s2 = await store.load("sess-A")
    assert s2 is not None
    assert s2.version == 1
