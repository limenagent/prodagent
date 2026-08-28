"""Composition proof — a sixth topology assembled from the public atoms.

``ReviewLoop`` occupies a grid cell none of the five shipped styles cover
(**版本化 KV × 轮流发言**): an author seeds a draft on a ``Board``, reviewers
amend it in strict rotation with optimistic-version writes, and the loop ends
by *business convergence* (an ``approved`` slot) before the hard cap.

Every piece is an atom, none of it is new machinery:

- shared state        ``Board`` (versioned slots, event-sourced capable)
- lifecycle           ``StageDriver`` subclass (crash guard, terminal event)
- termination         ``TerminationPolicy(business=BoardSatisfied, hard_cap=MaxRounds)``
- member discipline   reviewers return an amendment or ``None`` (a pass)

The point of the test: nothing here required touching a shipped style — the
atoms compose into a topology the framework never anticipated.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from prodagent.coordination.blackboard import Board, BoardWrite
from prodagent.coordination.infra.stage import (
    BoardSatisfied,
    MaxRounds,
    StageDriver,
    TerminationPolicy,
    TerminationReason,
)

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator, Awaitable, Callable


@dataclass
class ReviewEvent:
    """One landed amendment — the stream's intermediate event."""

    reviewer: str
    write: BoardWrite
    round: int


@dataclass
class ReviewCompleted:
    """Terminal event — why the loop stopped."""

    reason: TerminationReason


@dataclass
class ReviewLoopSpec:
    """Who reviews, in what order, and what 'done' means."""

    draft_key: str = "draft"
    seed: Any = None
    reviewers: dict[str, Callable[[Any], Awaitable[Any | None]]] = field(default_factory=dict)
    """name → async (current draft) → amended draft, or None to pass."""

    termination: TerminationPolicy = field(
        default_factory=lambda: TerminationPolicy(hard_cap=MaxRounds(max_rounds=10))
    )


class ReviewLoop(StageDriver[ReviewEvent | ReviewCompleted]):
    """Round-robin amendments over a versioned board slot."""

    def __init__(self, spec: ReviewLoopSpec) -> None:
        super().__init__()
        self._spec = spec
        self.board = Board()
        self._names = list(spec.reviewers)
        self._turn = 0

    async def _rounds(self) -> AsyncGenerator[ReviewEvent, None]:
        spec = self._spec
        await self.board.write(spec.draft_key, spec.seed)
        while True:
            round_num = self._turn // max(1, len(self._names))
            stop, reason = spec.termination.should_stop(self.board, next_round=round_num)
            if stop and reason is not None:
                self._reason = reason
                return

            name = self._names[self._turn % len(self._names)]
            version = self.board.version_of(spec.draft_key)
            amendment = await spec.reviewers[name](
                self.board.read([spec.draft_key])[spec.draft_key]
            )
            self._turn += 1
            if amendment is None:
                continue  # a pass — the reviewer had nothing to change

            write = BoardWrite(
                key=spec.draft_key, value=amendment, author=name, expected_version=version
            )
            await self.board.write(write.key, write.value, expected_version=write.expected_version)
            yield ReviewEvent(reviewer=name, write=write, round=round_num)

    def _completed(self, reason: TerminationReason) -> ReviewCompleted:
        return ReviewCompleted(reason=reason)


async def test_custom_topology_composes_from_public_atoms():
    """A Board × round-robin review loop stops on business convergence —
    the ``approved`` slot — before the hard cap fires."""

    async def tighten(draft: dict[str, Any]) -> dict[str, Any] | None:
        if draft.get("hedging"):
            return {**draft, "hedging": False}
        return None

    async def verify(draft: dict[str, Any]) -> dict[str, Any] | None:
        if draft.get("claims") == 3 and not draft.get("hedging"):
            return draft  # verified — write it back so 'approved' flips below
        return None

    # 'approved' flips when the draft is clean — the convergence predicate.
    def approved(board: Board) -> bool:
        draft = board.read(["draft"])["draft"]
        return bool(draft.get("hedging") is False and draft.get("claims") == 3)

    spec = ReviewLoopSpec(
        seed={"claims": 3, "hedging": True},
        reviewers={"style": tighten, "facts": verify},
        termination=TerminationPolicy(
            hard_cap=MaxRounds(max_rounds=8),
            business=BoardSatisfied(check=approved),
        ),
    )
    loop = ReviewLoop(spec)

    landed = [ev async for ev in loop.run() if isinstance(ev, ReviewEvent)]

    assert landed, "the style reviewer must have landed its de-hedging amendment"
    assert landed[0].reviewer == "style"
    assert spec.termination.hard_cap.max_rounds == 8  # cap configured but unused
    assert loop._reason is not None and loop._reason.reason == "convergence"
    assert spec.termination.business is not None
    final = loop.board.read(["draft"])["draft"]
    assert final == {"claims": 3, "hedging": False}
