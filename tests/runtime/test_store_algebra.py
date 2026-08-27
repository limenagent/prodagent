"""SharedStore algebra — the ``fingerprint()`` liveness contract across the
three stage stores: it must change when a mutating op succeeds and stay stable
when nothing moves. The drivers compare before/after a round on this to decide
``no_progress`` / ``no_contribution``."""

from __future__ import annotations

import pytest

from prodagent.backends.memory.dead_letter import InMemoryDeadLetterQueue
from prodagent.coordination.blackboard import Board
from prodagent.coordination.ensemble import FloorTurn, SharedFloor
from prodagent.coordination.work_queue import SharedQueue, WorkItem


class _NamedMember:
    """Minimal floor member — only ``name`` is needed to append a turn."""

    def __init__(self, name: str) -> None:
        self.name = name


@pytest.mark.asyncio
async def test_floor_fingerprint_changes_on_append_only():
    floor = SharedFloor(session_id="s")
    floor.add_member(_NamedMember("alice"))
    empty = floor.fingerprint()
    await floor.append(FloorTurn(speaker="alice", round=0, text="hi"))
    after_first = floor.fingerprint()
    assert after_first != empty
    await floor.append(FloorTurn(speaker="alice", round=0, text="again"))
    assert floor.fingerprint() != after_first
    # stable when nothing mutates
    assert floor.fingerprint() == floor.fingerprint()


@pytest.mark.asyncio
async def test_board_fingerprint_changes_on_write_only():
    board = Board()
    empty = board.fingerprint()
    await board.write("k", "v1")
    after_write = board.fingerprint()
    assert after_write != empty
    # stable when nothing mutates
    assert board.fingerprint() == board.fingerprint()


@pytest.mark.asyncio
async def test_queue_fingerprint_changes_on_claim_and_complete():
    q = SharedQueue([WorkItem("i0", 0)], dead_letter=InMemoryDeadLetterQueue(), lease_seconds=30.0)
    empty = q.fingerprint()
    await q.claim_next("w")
    after_claim = q.fingerprint()
    assert after_claim != empty  # pending 1→0, claimed 0→1
    await q.complete("i0")
    assert q.fingerprint() != after_claim  # claimed 1→0, completed 0→1
    assert q.fingerprint() == q.fingerprint()
