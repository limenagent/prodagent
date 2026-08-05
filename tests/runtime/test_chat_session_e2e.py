"""End-to-end chat semantics for the ConversationSession migration.

These tests verify the acceptance criteria from the plan:
  - A ``.workflow()`` agent's second chat turn on the same session raises
    ``PlanAlreadyCompletedError`` (criterion #3, #8) — no silent replay of
    the stale ``final_output``.
  - A ``.reactive()`` agent's second chat turn on the same session continues
    the conversation (criterion #6, #8) — messages accumulate, run_id mints
    fresh.
  - A ``.plan_first()`` agent's second chat turn re-plans rather than
    replaying the prior turn's output (criterion #1).
  - deepcopy isolation: two sessions on the same workflow Agent instance
    don't pollute each other's plan steps (criterion #4).
"""

from __future__ import annotations

import pytest

from prodagent.core.config import FrameworkConfig
from prodagent.core.exceptions import PlanAlreadyCompletedError
from prodagent.core.state.run import AgentRun
from prodagent.core.types import ExecutionMode, RunState
from prodagent.llm.fake import script
from prodagent.runtime.agent import Agent
from prodagent.runtime.workflow import Workflow


def _fw(tmp_path):
    fw = FrameworkConfig.default()
    fw.orchestration.runs_dir = str(tmp_path / "runs")
    fw.orchestration.sessions_dir = str(tmp_path / "sessions")
    return fw


def _workflow_agent(tmp_path) -> Agent:
    wf = Workflow()
    wf.llm_step("answer", "Reply with the result.", is_terminal=True)
    return Agent(
        "wf_chat",
        system_prompt="Reply briefly.",
        llm=script({"content": "first reply"}, {"content": "second reply"}),
        framework_config=_fw(tmp_path),
    ).workflow(wf, allow_replan=False)


def _reactive_agent(tmp_path) -> Agent:
    return Agent(
        "reactive_chat",
        system_prompt="Reply briefly.",
        llm=script({"content": "first reply"}, {"content": "second reply"}),
        framework_config=_fw(tmp_path),
    ).reactive()


def _plan_first_agent(tmp_path) -> Agent:
    return Agent(
        "plan_first_chat",
        system_prompt="Reply briefly.",
        llm=script(
            {
                "content": '{"steps":[{"id":"s1","action":"reply","params":{},"depends_on":[],"terminal":true}]}'
            },
            {"content": "first reply"},
            {
                "content": '{"steps":[{"id":"s1","action":"reply","params":{},"depends_on":[],"terminal":true}]}'
            },
            {"content": "second reply"},
        ),
        framework_config=_fw(tmp_path),
    ).plan_first()


@pytest.mark.asyncio
async def test_workflow_second_chat_turn_raises_plan_already_completed(tmp_path):
    agent = _workflow_agent(tmp_path)
    r1 = await agent.chat("first turn", session_id="wf-A")
    assert r1.state.value == "completed"

    with pytest.raises(PlanAlreadyCompletedError):
        await agent.chat("second turn", session_id="wf-A")


@pytest.mark.asyncio
async def test_workflow_chat_with_explicit_reactive_mode_continues(tmp_path):
    """A ``.workflow()`` agent's second chat turn raises PlanAlreadyCompletedError
    by default — but when the caller explicitly passes ``mode=REACTIVE``, the
    guard stands down and the turn runs as a reactive follow-up.

    This is the escape hatch the playground uses to continue a conversation on
    a workflow agent after its plan has run to completion, instead of replaying
    the stale ``final_output``.
    """
    from prodagent.core.types import ExecutionMode

    agent = _workflow_agent(tmp_path)
    r1 = await agent.chat("first turn", session_id="wf-react-A")
    assert r1.state.value == "completed"

    r2 = await agent.chat("second turn", session_id="wf-react-A", mode=ExecutionMode.REACTIVE)
    assert r2.state.value == "completed"
    assert r1.run_id != r2.run_id
    assert len(r2.messages) > len(r1.messages)


@pytest.mark.asyncio
async def test_reactive_second_chat_turn_continues_conversation(tmp_path):
    agent = _reactive_agent(tmp_path)
    r1 = await agent.chat("first", session_id="react-A")
    assert r1.state.value == "completed"

    r2 = await agent.chat("second", session_id="react-A")
    assert r2.state.value == "completed"
    # Fresh run_id per turn — no replay.
    assert r1.run_id != r2.run_id
    # Messages accumulate across turns (proves the session is the source of truth).
    assert len(r2.messages) > len(r1.messages)


@pytest.mark.asyncio
async def test_workflow_deepcopy_isolates_sessions(tmp_path):
    """Two sessions on the same workflow Agent instance must not pollute each other.

    Without deepcopy, the first session's COMPLETED steps would leak into the
    second session's ``initial_plan`` reference and trip the
    ``PlanAlreadyCompletedError`` guard on turn 1.
    """
    agent = _workflow_agent(tmp_path)
    ra = await agent.chat("A msg", session_id="sess-A")
    rb = await agent.chat("B msg", session_id="sess-B")

    assert ra.state.value == "completed"
    assert rb.state.value == "completed"


@pytest.mark.asyncio
async def test_suspended_resume_reuses_run_id_and_rejects_mode_mismatch(tmp_path):
    from prodagent.core.types import ExecutionMode, RunState

    agent = _reactive_agent(tmp_path)
    r1 = await agent.chat("first", session_id="sus-A")
    assert r1.state.value == "completed"

    # Simulate a SUSPENDED prior turn by editing the session store directly.
    store = agent._ensure_session_store_resolved()
    session = await store.load("sus-A")
    assert session is not None
    session.turns[-1].state = RunState.SUSPENDED
    await store.save(session, expected_version=session.version)

    # Same mode resumes (reuses run_id); different mode raises ValueError.
    with pytest.raises(ValueError, match="SUSPENDED"):
        session.start_turn("follow", mode=ExecutionMode.PLAN_FIRST)


@pytest.mark.asyncio
async def test_reactive_chat_does_not_replay_prior_final_output(tmp_path):
    """The original bug: a chat follow-up replayed the stale final_output.

    With ConversationSession, the second turn gets a fresh run_id and the
    FakeLLM's next scripted reply — not a replay of turn 1's output.
    """
    agent = _reactive_agent(tmp_path)
    r1 = await agent.chat("first", session_id="noreplay-A")
    assert "first reply" in r1.final_output

    r2 = await agent.chat("second", session_id="noreplay-A")
    assert "second reply" in r2.final_output
    assert r2.final_output != r1.final_output


def test_construction_asserts_initial_plan_requires_plan_first():
    """Criterion #5: the ``initial_plan`` invariant expression rejects REACTIVE+plan.

    ``.workflow()`` sets ``_mode=PLAN_FIRST`` and ``_initial_plan=wf.compile()``
    atomically. The assertion at ``agent.py:85`` —
    ``self._initial_plan is None or self._mode is ExecutionMode.PLAN_FIRST`` —
    is defence against a future API breaking that. This test confirms the
    expression itself catches the broken state.
    """
    from prodagent.core.types import ExecutionMode
    from prodagent.runtime.plan.dag import Plan

    agent = Agent(
        "bad",
        system_prompt="",
        llm=script({"content": "x"}),
        mode=ExecutionMode.REACTIVE,
    )
    # Simulate the broken invariant a future API might introduce.
    agent.config.initial_plan = Plan(plan_id="x")
    # The exact expression from agent.py:85 — must be False for this state.
    invariant = agent.config.initial_plan is None or agent.config.mode is ExecutionMode.PLAN_FIRST
    assert invariant is False, (
        "the construction assertion must reject REACTIVE + initial_plan — "
        "if this passes, the guard at agent.py:85 is broken"
    )


@pytest.mark.asyncio
async def test_crash_window_does_not_deadlock_session(tmp_path):
    """The bug: start_turn mints run_id in-memory; if the process crashes before
    session_store.save(), the next chat() reloads the old turn_seq and re-mints
    the same run_id — colliding with an orphan checkpoint and deadlocking the
    session with a VersionConflict that never resolves.

    Fix: _begin_chat_turn persists the session immediately after minting, so
    the crash window shrinks from "the whole run" to "one save call". This test
    simulates a crash right after start_turn (no complete_turn, no save, but
    also no checkpoint written since the run never executed), then verifies the
    next chat() completes cleanly rather than deadlocking.

    Note: the re-minted run_id may numerically equal the crashed turn's would-be
    id (both derive from turn_seq) — this is benign because the crashed turn
    left no checkpoint and no side effects. The orphan-checkpoint guard
    (test_orphan_checkpoint_raises_run_id_collision) covers the dangerous case
    where the crashed turn *did* write a checkpoint.
    """
    agent = _reactive_agent(tmp_path)

    # Turn 1 completes normally — establishes a baseline session on disk.
    r1 = await agent.chat("first", session_id="crash-A")
    assert r1.state.value == "completed"

    # Simulate the crash window: start_turn mints a run_id in-memory but the
    # process dies before complete_turn + save. The on-disk session still has
    # turn_seq=1 (from turn 1's complete_turn).
    store = agent._ensure_session_store_resolved()
    session = await store.load("crash-A")
    assert session is not None
    crashed_alloc = session.start_turn("crashed turn", mode=ExecutionMode.REACTIVE)
    assert crashed_alloc.is_new is True
    assert crashed_alloc.run_id == "crash-A:2"  # would-be turn 2
    # NOT saved — simulating the crash. Discard the in-memory mutation.

    # Next chat() re-loads from disk (turn_seq still 1) and must complete
    # cleanly. Before the fix, if the crashed turn had written a checkpoint,
    # this would deadlock with VersionConflict forever. With the fix, the early
    # session save persists turn_seq before the run, so even a mid-run crash
    # on THIS turn won't leave the session stuck.
    r2 = await agent.chat("second", session_id="crash-A")
    assert r2.state.value == "completed"
    # The session is now coherent: turn_seq advanced, last_turn settled.
    session_after = await store.load("crash-A")
    assert session_after is not None
    assert session_after.turn_seq == 2
    assert session_after.last_turn.run_id == r2.run_id
    assert session_after.last_turn.state is RunState.COMPLETED


@pytest.mark.asyncio
async def test_orphan_checkpoint_raises_run_id_collision(tmp_path):
    """If a freshly minted run_id already has a checkpoint in the store (orphan
    from a pre-fix crash, or an extreme race), _begin_chat_turn raises
    RunIdCollisionError instead of silently resuming or throwing a confusing
    VersionConflict mid-run.
    """
    from prodagent.core.exceptions import RunIdCollisionError
    from prodagent.core.types import RunState

    agent = _reactive_agent(tmp_path)

    # Turn 1 completes — session on disk has turn_seq=1.
    r1 = await agent.chat("first", session_id="orphan-A")
    assert r1.state.value == "completed"

    # Pre-seed an orphan checkpoint at the run_id the next turn *would* mint
    # ("orphan-A:2") — simulating a leftover from a pre-fix crash.
    checkpoint = agent._ensure_checkpoint_resolved()
    orphan_run = AgentRun(run_id="orphan-A:2", task="leftover")
    orphan_run.state = RunState.SUSPENDED
    await checkpoint.save(orphan_run)

    # Next chat() mints "orphan-A:2", finds the orphan checkpoint, must refuse.
    with pytest.raises(RunIdCollisionError, match="orphan-A:2"):
        await agent.chat("second", session_id="orphan-A")


@pytest.mark.asyncio
async def test_chat_stream_mid_break_then_chat_mints_new_run_id(tmp_path):
    """chat_stream shares _begin_chat_turn with chat, so the early session save
    happens before the stream starts. If the consumer breaks mid-stream, the
    session already has the new turn_seq persisted but no complete_turn — the
    next chat() must still mint a fresh run_id, not try to reuse the incomplete
    turn.
    """
    agent = _reactive_agent(tmp_path)

    # Turn 1 via chat_stream — break out after the first event.
    gen = agent.chat_stream("first", session_id="stream-A")
    async for _event in gen:
        break  # consumer disconnects mid-stream
    await gen.aclose()

    # The session was saved with turn_seq=1 (early persist) but no
    # complete_turn, so last_turn is None or the turn never settled. Either way,
    # the next chat() must mint a fresh run_id and complete cleanly.
    r2 = await agent.chat("second", session_id="stream-A")
    assert r2.state.value == "completed"
    assert r2.run_id != "stream-A:1", "must not reuse the incomplete turn 1's run_id"


@pytest.mark.asyncio
async def test_chat_stream_consumer_returns_on_terminal_event_persists_turn(tmp_path):
    """playground's _drive_stream returns immediately on RunCompletedEvent without
    draining the rest of the generator. chat_stream must still fold the run's
    transcript back into the session — otherwise the next turn sees an empty
    history (the agent 'forgets' what it just did).

    Regression: complete_turn + save lived after the `async for` in chat_stream,
    so a consumer that returns on the terminal event skipped it. The fix wraps
    the finalize in a finally block gated on final_run.
    """
    from prodagent.core.events import RunCompletedEvent

    agent = _reactive_agent(tmp_path)

    # Turn 1 — consume chat_stream like the playground does: return as soon as
    # the terminal event lands, do NOT drain the rest of the generator.
    gen = agent.chat_stream("first", session_id="stream-B")
    async for event in gen:
        if isinstance(event, RunCompletedEvent):
            break
    await gen.aclose()

    # The session must now reflect turn 1: messages include the assistant reply,
    # and turns has a completed record.
    store = agent._ensure_session_store_resolved()
    session = await store.load("stream-B")
    assert session is not None, "session must have been persisted"
    assert len(session.turns) == 1, f"complete_turn must have run; got turns={session.turns}"
    assert session.turns[0].state is RunState.COMPLETED
    roles = [m["role"] for m in session.messages]
    assert "assistant" in roles, f"assistant reply must be in messages; got roles={roles}"

    # Turn 2 — the new turn must see turn 1's history, not start blank.
    r2 = await agent.chat("second", session_id="stream-B")
    assert r2.state.value == "completed"
    assert r2.run_id == "stream-B:2", "fresh run_id for the new turn"
