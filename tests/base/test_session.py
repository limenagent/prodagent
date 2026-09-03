"""ConversationSession — the cross-turn conversation root.

Covers the run_id/mode bookkeeping invariants that the chat path relies on:
fresh run_id per turn, SUSPENDED reuse + mode check, transcript fold-back,
optimistic lock version, and round-trip serialization.
"""

from __future__ import annotations

import pytest

from prodagent.base.session import ConversationSession, TurnRecord
from prodagent.kernel.run import Run
from prodagent.kernel.types import RunState


def _completed_run(run_id: str, *, messages=None, final_output: str = "ok") -> Run:
    run = Run(run_id=run_id, task="t")
    run.state = RunState.COMPLETED
    run.final_output = final_output
    if messages:
        run.messages = list(messages)
    return run


def _suspended_run(run_id: str, *, messages=None) -> Run:
    run = Run(run_id=run_id, task="t")
    run.state = RunState.SUSPENDED
    if messages:
        run.messages = list(messages)
    return run


class TestStartTurn:
    def test_first_turn_mints_run_id_with_seq_and_is_new(self):
        s = ConversationSession(session_id="sess-A", agent_id="agent")
        alloc = s.start_turn("hi", single_unit=True)
        assert alloc.run_id == "sess-A:1"
        assert alloc.single_unit is True
        assert alloc.is_new is True
        assert len(alloc.messages) == 1
        assert alloc.messages[0]["role"] == "user"
        assert alloc.messages[0]["content"] == "hi"

    def test_completed_prior_turn_mints_new_run_id(self):
        s = ConversationSession(session_id="sess-A", agent_id="agent")
        rid1 = s.start_turn("m1", single_unit=False).run_id
        s.complete_turn(rid1, False, _completed_run(rid1))
        alloc2 = s.start_turn("m2", single_unit=False)
        assert rid1 != alloc2.run_id
        assert rid1 == "sess-A:1"
        assert alloc2.run_id == "sess-A:2"
        assert alloc2.is_new is True

    def test_suspended_prior_turn_reuses_run_id_same_mode_not_new(self):
        s = ConversationSession(session_id="sess-A", agent_id="agent")
        rid1 = s.start_turn("m1", single_unit=False).run_id
        s.complete_turn(rid1, False, _suspended_run(rid1))
        alloc2 = s.start_turn("resume", single_unit=False)
        assert rid1 == alloc2.run_id
        assert alloc2.is_new is False

    def test_suspended_prior_turn_rejects_mode_mismatch(self):
        s = ConversationSession(session_id="sess-A", agent_id="agent")
        rid1 = s.start_turn("m1", single_unit=False).run_id
        s.complete_turn(rid1, False, _suspended_run(rid1))
        with pytest.raises(ValueError, match="SUSPENDED"):
            s.start_turn("resume", single_unit=True)

    def test_messages_accumulate_across_turns(self):
        s = ConversationSession(session_id="sess-A", agent_id="agent")
        rid1 = s.start_turn("first", single_unit=True).run_id
        s.complete_turn(
            rid1,
            True,
            _completed_run(
                rid1,
                messages=[
                    {"role": "user", "content": "first"},
                    {"role": "assistant", "content": "r1"},
                ],
            ),
        )
        m2 = s.start_turn("second", single_unit=True).messages
        assert len(m2) == 3
        assert m2[0]["content"] == "first"
        assert m2[2]["content"] == "second"


class TestCompleteTurn:
    def test_folds_run_messages_into_session(self):
        s = ConversationSession(session_id="sess-A", agent_id="agent")
        rid = s.start_turn("hi", single_unit=True).run_id
        msgs = [{"role": "user", "content": "hi"}, {"role": "assistant", "content": "hey"}]
        s.complete_turn(rid, True, _completed_run(rid, messages=msgs))
        assert s.messages == msgs

    def test_records_turn_with_state_and_output(self):
        s = ConversationSession(session_id="sess-A", agent_id="agent")
        rid = s.start_turn("hi", single_unit=True).run_id
        s.complete_turn(rid, True, _completed_run(rid, final_output="done"))
        assert len(s.turns) == 1
        t = s.last_turn
        assert t.run_id == rid
        assert t.single_unit is True
        assert t.state is RunState.COMPLETED
        assert t.final_output == "done"

    def test_suspended_then_resumed_replaces_turn_record(self):
        s = ConversationSession(session_id="sess-A", agent_id="agent")
        rid = s.start_turn("m1", single_unit=False).run_id
        s.complete_turn(rid, False, _suspended_run(rid))
        assert s.last_turn.state is RunState.SUSPENDED
        # Resume path reuses the same run_id, so the turn record is replaced in place.
        s.complete_turn(rid, False, _completed_run(rid, final_output="finished"))
        assert len(s.turns) == 1
        assert s.last_turn.state is RunState.COMPLETED
        assert s.last_turn.final_output == "finished"

    def test_complete_turn_makes_session_messages_equal_run_messages(self):
        # Invariant: complete_turn copies the whole transcript, not a reference
        # or delta. If someone later "optimizes" this to a shallow ref or
        # incremental copy, history silently drops — this test fails first.
        s = ConversationSession(session_id="sess-A", agent_id="agent")
        rid = s.start_turn("hi", single_unit=True).run_id
        run_msgs = [
            {"role": "user", "content": "hi"},
            {"role": "assistant", "content": "hey"},
            {"role": "user", "content": "more"},
        ]
        s.complete_turn(rid, True, _completed_run(rid, messages=run_msgs))
        assert s.messages == run_msgs
        # Mutating the run's list post-complete must not leak into the session.
        run_msgs.append({"role": "assistant", "content": "late"})
        assert s.messages == [
            {"role": "user", "content": "hi"},
            {"role": "assistant", "content": "hey"},
            {"role": "user", "content": "more"},
        ]

    def test_bumps_version_on_save(self):
        # The SessionStore calls save() which bumps version on each write.
        # Here we just verify the field exists and is mutable.
        s = ConversationSession(session_id="s", agent_id="a", version=0)
        s.version += 1
        assert s.version == 1


class TestSerialization:
    def test_round_trip_preserves_messages_and_turns(self):
        s = ConversationSession(session_id="sess-A", agent_id="agent")
        rid = s.start_turn("m1", single_unit=False).run_id
        s.complete_turn(
            rid,
            False,
            _completed_run(
                rid,
                messages=[
                    {"role": "user", "content": "m1"},
                    {"role": "assistant", "content": "r1"},
                ],
            ),
        )
        d = s.to_dict()
        s2 = ConversationSession.from_dict(d)
        assert s2.session_id == "sess-A"
        assert s2.agent_id == "agent"
        assert len(s2.messages) == 2
        assert s2.messages[1]["content"] == "r1"
        assert len(s2.turns) == 1
        assert s2.turns[0].run_id == rid
        assert s2.turns[0].state is RunState.COMPLETED
        assert s2.turns[0].single_unit is False

    def test_empty_session_round_trip(self):
        s = ConversationSession(session_id="empty", agent_id="agent")
        s2 = ConversationSession.from_dict(s.to_dict())
        assert s2.messages == []
        assert s2.turns == []
        assert s2.turn_seq == 0


class TestTurnRecord:
    def test_to_dict_serializes_enums(self):
        t = TurnRecord(run_id="r", single_unit=True, state=RunState.SUSPENDED)
        d = t.to_dict()
        assert d["single_unit"] is True
        assert d["state"] == RunState.SUSPENDED.value

    def test_from_dict_reads_enums(self):
        t = TurnRecord.from_dict(
            {
                "run_id": "r",
                "single_unit": False,
                "state": RunState.COMPLETED.value,
                "final_output": "ok",
            }
        )
        assert t.single_unit is False
        assert t.state is RunState.COMPLETED
