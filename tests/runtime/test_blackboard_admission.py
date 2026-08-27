"""Blackboard write admission — per-key contracts reject dirty values without
killing the board; version conflicts isolate the losing expert; renders are
bounded."""

from __future__ import annotations

from prodagent.coordination.blackboard import (
    BlackboardSpec,
    BoardWrite,
    BoardWriteEvent,
    Trigger,
    blackboard_stream,
)
from prodagent.coordination.infra.stage import MaxRounds, TerminationPolicy
from prodagent.coordination.messaging.contract import MessageContract
from prodagent.kernel.bus import BlockingResult, Gate, HookRegistry


class _OnceMember:
    """Contributes a scripted write on round 0, then nothing."""

    def __init__(self, name: str, write: BoardWrite | None) -> None:
        self.name = name
        self._write = write
        self.fired = False

    async def try_contribute(self, board, *, trigger) -> BoardWrite | None:
        if self.fired:
            return None
        self.fired = True
        return self._write


async def _collect(spec: BlackboardSpec):
    events = []
    async for event in blackboard_stream(spec):
        events.append(event)
    return events


def _spec(members, **kwargs) -> BlackboardSpec:
    experts = {m.name: m for m in members}
    return BlackboardSpec(
        experts=experts,
        triggers={"kickoff": Trigger(name="kickoff", keys=[], experts=list(experts))},
        termination=TerminationPolicy(hard_cap=MaxRounds(max_rounds=3)),
        **kwargs,
    )


async def test_per_key_contract_rejects_dirty_value_board_survives():
    dirty = _OnceMember(
        "dirty", BoardWrite(key="answer", value={"confidence": "high??"}, author="dirty")
    )
    clean = _OnceMember(
        "clean", BoardWrite(key="notes", value="structured summary", author="clean")
    )
    contracts = {
        "answer": MessageContract(required_fields=["confidence"], field_types={"confidence": float})
    }

    events = await _collect(_spec([dirty, clean], contracts=contracts))

    writes = [e.write for e in events if isinstance(e, BoardWriteEvent)]
    keys = {w.key for w in writes}
    assert "notes" in keys  # clean write landed
    assert "answer" not in keys  # dirty write rejected, not recorded


async def test_undeclared_key_admitted_as_is():
    free = _OnceMember("free", BoardWrite(key="scratchpad", value="anything goes", author="free"))

    events = await _collect(_spec([free]))

    writes = [e.write for e in events if isinstance(e, BoardWriteEvent)]
    assert any(w.key == "scratchpad" for w in writes)


async def test_gate_veto_skips_write():
    registry = HookRegistry()

    async def veto(**data):
        handoff = data["handoff_data"]
        if handoff["next_action"] == "write" and "poison" in str(handoff["result_data"]):
            return BlockingResult(blocked=True, reason="poisoned write")
        return BlockingResult(blocked=False)

    registry.register_checker(Gate.AGENT_HANDOFF, veto)
    poisoned = _OnceMember(
        "bad", BoardWrite(key="state", value="poison: rm -rf everything", author="bad")
    )
    good = _OnceMember("good", BoardWrite(key="summary", value="fine", author="good"))

    events = await _collect(_spec([poisoned, good], hooks=registry))

    writes = [e.write for e in events if isinstance(e, BoardWriteEvent)]
    assert {w.key for w in writes} == {"summary"}


async def test_version_conflict_isolates_loser_board_survives():
    from prodagent.backends.memory.dead_letter import InMemoryDeadLetterQueue

    class _SpyDLQ(InMemoryDeadLetterQueue):
        def __init__(self) -> None:
            super().__init__(max_retries=3)
            self.calls: list[tuple[str, str]] = []

        async def on_failure(self, message_id, payload, error):
            self.calls.append((message_id, error))
            return await super().on_failure(message_id, payload, error)

    dlq = _SpyDLQ()

    class _StaleMember:
        """Writes with a stale expected_version — races a winner that already
        bumped the slot. Previously this killed the whole board via the
        StageDriver crash guard; now the loser is dropped and dead-lettered."""

        name = "stale"

        def __init__(self) -> None:
            self.fired = False

        async def try_contribute(self, board, *, trigger):
            if self.fired:
                return None
            self.fired = True
            await board.write("answer", "winner got there first")
            return BoardWrite(key="answer", value="stale write", author="stale", expected_version=0)

    stale = _StaleMember()
    other = _OnceMember("other", BoardWrite(key="summary", value="fine", author="other"))

    events = await _collect(_spec([stale, other], dead_letter=dlq))

    # Board completed — no terminal error event from a crash guard.
    assert not any(getattr(e, "reason", None) and e.reason.reason == "error" for e in events)
    # The stale write was dead-lettered, the independent write landed.
    assert any(mid.endswith("answer") for mid, _ in dlq.calls)
    writes = [e.write for e in events if isinstance(e, BoardWriteEvent)]
    assert any(w.key == "summary" for w in writes)
    assert all(w.key != "answer" or w.value != "stale write" for w in writes)


async def test_oversized_free_text_value_truncated_at_admission():
    windbag = _OnceMember("windbag", BoardWrite(key="notes", value="y" * 10_000, author="windbag"))

    events = await _collect(_spec([windbag]))

    writes = [e.write for e in events if isinstance(e, BoardWriteEvent)]
    recorded = next(w for w in writes if w.key == "notes")
    assert len(recorded.value) <= 2100
    assert "truncated" in recorded.value


def test_render_value_bounds_non_strings():
    from prodagent.coordination.blackboard import _render_value

    long_dict = {"k": "v" * 5000}
    rendered = _render_value(long_dict, 200)
    assert len(rendered) <= 250
    assert "truncated" in rendered
    assert _render_value("short", 200) == "short"
