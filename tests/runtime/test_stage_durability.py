"""Stage durability — floor and board survive a crash and resume from their
event logs, the same contract the work queue already had (test_work_queue_
durability.py). The interrupt is a stream abandoned mid-run: the generator is
closed at a yield point, exactly the state a kill -9 leaves behind."""

from __future__ import annotations

from contextlib import aclosing
from typing import TYPE_CHECKING

import pytest

from prodagent.backends.factory import in_memory_event_log
from prodagent.coordination.blackboard import (
    BlackboardSpec,
    Board,
    BoardWrite,
    Trigger,
    blackboard_stream,
)
from prodagent.coordination.ensemble import (
    EnsembleSpec,
    FloorTurn,
    FloorTurnEvent,
    SharedFloor,
    ensemble_stream,
)
from prodagent.coordination.infra.stage import MaxRounds, TerminationPolicy
from prodagent.kernel.budget import BudgetLedger, HardBudget

if TYPE_CHECKING:
    from prodagent.ports import EventLog


class _ScriptedMember:
    """Hand-rolled FloorMember speaking scripted turns."""

    def __init__(self, name: str, texts: list[str]) -> None:
        self.name = name
        self._texts = list(texts)

    async def speak(self, floor, *, round_num: int) -> FloorTurn:
        text = self._texts.pop(0) if self._texts else ""
        return FloorTurn(speaker=self.name, round=round_num, text=text)


class _WriteOnceExpert:
    """Writes its key once, passes once the slot has any version — so a
    restored board makes the fresh expert pass instead of rewriting."""

    def __init__(self, name: str, key: str) -> None:
        self.name = name
        self._key = key

    async def try_contribute(self, board, *, trigger) -> BoardWrite | None:
        if board.version_of(self._key) > 0:
            return None
        return BoardWrite(key=self._key, value=f"value-from-{self.name}", author=self.name)


def _ensemble_spec(members, *, event_log: EventLog | None = None, run_id: str = "") -> EnsembleSpec:
    return EnsembleSpec(
        members=members,
        topic="resume test",
        termination=TerminationPolicy(hard_cap=MaxRounds(max_rounds=3)),
        budget=BudgetLedger(max=HardBudget(max_turns=50, max_seconds=60.0)),
        event_log=event_log,
        run_id=run_id,
    )


@pytest.mark.asyncio
async def test_ensemble_resumes_floor_from_event_log_after_interrupt():
    log = in_memory_event_log()

    # Run 1: two members speak, the stream is abandoned after the second turn.
    first_run = _ensemble_spec(
        [
            _ScriptedMember("a", ["a-r0", "a-r1", "a-r2"]),
            _ScriptedMember("b", ["b-r0", "b-r1", "b-r2"]),
        ],
        event_log=log,
        run_id="ens-1",
    )
    seen: list[FloorTurn] = []
    async with aclosing(ensemble_stream(first_run)) as stream:
        async for event in stream:
            if isinstance(event, FloorTurnEvent):
                seen.append(event.turn)
            if len(seen) == 2:
                break
    assert [t.text for t in seen] == ["a-r0", "b-r0"]
    recorded = await log.get_events("ens-1")
    assert len(recorded) == 2, "each completed turn must be durable before its event yields"

    # Run 2: same log and run_id, fresh member objects — the floor resumes.
    second_run = _ensemble_spec(
        [
            _ScriptedMember("a", ["a-r1", "a-r2"]),
            _ScriptedMember("b", ["b-r1", "b-r2"]),
        ],
        event_log=log,
        run_id="ens-1",
    )
    turns: list[FloorTurn] = []
    final_transcript: list[FloorTurn] = []
    async for event in ensemble_stream(second_run):
        if isinstance(event, FloorTurnEvent):
            turns.append(event.turn)
        else:
            final_transcript = event.final_transcript

    # Continuation, not restart: run 1 ended mid-round-0 (after b spoke), so
    # run 2's first NEW turn opens round 1 — a fresh floor would start at 0.
    assert turns[0].round == 1, "restored floor must continue the round count"

    # The completed floor carries the WHOLE debate: run-1 turns verbatim
    # (same turn ids — they were rebuilt from the log, not re-spoken) plus
    # run-2's continuation. MaxRounds=3 → rounds 0-2 → six turns total.
    assert [t.turn_id for t in final_transcript[:2]] == [t.turn_id for t in seen]
    assert [t.text for t in final_transcript] == [
        "a-r0",
        "b-r0",
        "a-r1",
        "b-r1",
        "a-r2",
        "b-r2",
    ]
    # And the log holds exactly the six turns — restored ones are not
    # re-recorded.
    assert len(await log.get_events("ens-1")) == 6


@pytest.mark.asyncio
async def test_blackboard_resumes_board_from_event_log_after_interrupt():
    log = in_memory_event_log()

    def spec(experts):
        return BlackboardSpec(
            experts=experts,
            triggers={
                "kickoff": Trigger(name="kickoff", keys=[], experts=list(experts), mode="event"),
            },
            termination=TerminationPolicy(hard_cap=MaxRounds(max_rounds=5)),
            event_log=log,
            run_id="bb-1",
        )

    # Run 1: abandoned after the first write lands.
    writes = 0
    async with aclosing(
        blackboard_stream(
            spec(
                {
                    "alice": _WriteOnceExpert("alice", "field_a"),
                    "bob": _WriteOnceExpert("bob", "field_b"),
                }
            )
        )
    ) as stream:
        async for event in stream:
            if type(event).__name__ == "BoardWriteEvent":
                writes += 1
            if writes == 1:
                break
    assert len(await log.get_events("bb-1")) == 1

    # Run 2: fresh experts; the restored slot makes its writer pass instead
    # of writing again — versions prove restore, not rewrite.
    completed = None
    async for event in blackboard_stream(
        spec(
            {
                "alice2": _WriteOnceExpert("alice2", "field_a"),
                "bob2": _WriteOnceExpert("bob2", "field_b"),
            }
        )
    ):
        completed = event
    slots = completed.board_snapshot["slots"]
    assert slots["field_a"]["value"] == "value-from-alice", "run-1 value survives verbatim"
    assert slots["field_a"]["version"] == 1, "restored slot must not be rewritten"
    assert slots["field_b"]["value"] == "value-from-bob2"


@pytest.mark.asyncio
async def test_floor_restore_reattaches_members_and_turn_ids():
    log = in_memory_event_log()
    floor = SharedFloor(session_id="s", topic="t", event_log=log, run_id="f-1")
    member = _ScriptedMember("a", [])
    floor.add_member(member)
    turn = FloorTurn(speaker="a", round=0, text="keep my id", turn_id="fixed-id")
    await floor.append(turn)

    restored = await SharedFloor.restore(log, "f-1", session_id="s", topic="t", members=[member])
    assert [t.turn_id for t in restored.transcript] == ["fixed-id"]
    assert restored.members["a"] is member, "live members re-attach, never serialize"


@pytest.mark.asyncio
async def test_board_restore_preserves_versions_for_optimistic_writes():
    log = in_memory_event_log()
    board = Board(event_log=log, run_id="b-1")
    await board.write("k", "v1")
    await board.write("k", "v2")

    restored = await Board.restore(log, "b-1")
    assert restored.version_of("k") == 2
    # An optimistic write against the restored version succeeds — the resume
    # path continues exactly where the crash left off.
    assert await restored.write("k", "v3", expected_version=2) == 3
