"""SharedFloor — the shared transcript all ensemble members read and write.

Unlike ``peers=`` (linear baton-pass, each prior run terminated) or ``agents=``
(vertical delegation, parent waits for child result), an ensemble keeps every
member on the same floor: one persistent transcript, all members read it, each
writes its own turns. Substrate for debate / conversation / role-play.

- ``FloorMember`` is a *protocol*, not ``Agent`` — a hand-rolled ``messages``
  list qualifies. Personality/memory isolation is the member's own business;
  the floor is what they share.
- ``FloorTurn`` carries speaker metadata + stance/addressed_to so a moderator
  or projection can reason about it. ``tool_calls`` visibility is a
  :class:`FloorProjection` decision, not a ``FloorTurn`` decision.
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

from prodagent.runtime.coordination._store import SharedStore

if TYPE_CHECKING:
    from prodagent.core.types import ToolCall


__all__ = ["FloorTurn", "FloorMember", "SharedFloor"]


@dataclass
class FloorTurn:
    """One member's utterance on the shared floor."""

    speaker: str
    """Member name — must match a key in ``SharedFloor.members``."""

    round: int
    """Round index this turn belongs to (0-based)."""

    text: str
    """The utterance itself. Empty string = pass."""

    addressed_to: list[str] = field(default_factory=list)
    """Who the speaker is talking to (empty = the floor)."""

    stance: str | None = None
    """Optional stance label — ``support``/``refute`` for debates."""

    tool_calls: list[ToolCall] = field(default_factory=list)
    """Tools called this turn. Visibility is a FloorProjection call."""

    cost_usd: float = 0.0
    """Spend attributed to this turn — folded into SharedBudget."""

    tokens: int = 0
    """Token spend attributed to this turn (input + output) — folded into
    SharedBudget alongside ``cost_usd``. Without it the budget's token axis is
    silently unenforced for ensemble runs."""

    elapsed_s: float = 0.0
    """Wall-clock seconds this turn took."""

    turn_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    """Stable id — projection / hooks / checkpoint correlation."""

    created_at: float = field(default_factory=time.monotonic)
    """Monotonic timestamp — ordering and timeout accounting."""

    def is_pass(self) -> bool:
        """Pass turn (empty text, no tool calls) — member chose not to speak."""
        return not self.text and not self.tool_calls


@runtime_checkable
class FloorMember(Protocol):
    """What it takes to join an ensemble: a ``name`` + async ``speak()``.

    A full :class:`~prodagent.runtime.agent.Agent` works (via
    :class:`AgentFloorMember`); a hand-rolled ``messages`` list works too.
    """

    name: str

    async def speak(self, floor: SharedFloor, *, round_num: int) -> FloorTurn:
        """Produce this member's turn for ``round_num``. Read
        ``floor.transcript`` (already projected for this viewer by the
        pipeline) and return a :class:`FloorTurn`. A pass turn (empty text,
        no tools) sits the round out."""
        ...


@dataclass
class SharedFloor(SharedStore):
    """The shared transcript all ensemble members read and write.

    Lifetime is independent of any single member's run — persists across
    rounds, outliving individual ``AgentRun`` instances the way a chat room
    outlives any one message.
    """

    session_id: str
    """Stable id — correlates to checkpoint / event log / session store."""

    members: dict[str, FloorMember] = field(default_factory=dict)
    """name → member. Insertion order preserved for round-robin."""

    transcript: list[FloorTurn] = field(default_factory=list)
    """All turns, in order. Source of truth for 'what was said'."""

    topic: str = ""
    """The floor's subject — injected into each member's [FLOOR] block."""

    started_at: float = field(default_factory=time.monotonic)
    """Monotonic start — basis for the shared wall-clock budget."""

    def add_member(self, member: FloorMember) -> None:
        if member.name in self.members:
            raise ValueError(
                f"Floor member {member.name!r} already exists on floor "
                f"{self.session_id!r} — names must be unique"
            )
        self.members[member.name] = member

    def append(self, turn: FloorTurn) -> None:
        """Record a completed turn. Validates speaker membership, not ordering —
        the pipeline sequences."""
        if turn.speaker not in self.members:
            raise ValueError(
                f"Turn speaker {turn.speaker!r} is not a floor member — "
                f"known: {list(self.members.keys())}"
            )
        self.transcript.append(turn)

    def round_count(self) -> int:
        """Highest round index seen + 1, or 0 if empty. Partial rounds count."""
        if not self.transcript:
            return 0
        return max(t.round for t in self.transcript) + 1

    def turns_for(self, speaker: str) -> list[FloorTurn]:
        """All turns by ``speaker``, in order."""
        return [t for t in self.transcript if t.speaker == speaker]

    def last_turn_by(self, speaker: str) -> FloorTurn | None:
        """Most recent turn by ``speaker``, or None."""
        for turn in reversed(self.transcript):
            if turn.speaker == speaker:
                return turn
        return None

    def recent_turns(self, *, limit: int) -> list[FloorTurn]:
        """Last ``limit`` turns, oldest-first. Caps how much history each
        member sees — mirrors ``prior_output_max_chars`` in :class:`HandoffPacket`."""
        if limit <= 0 or not self.transcript:
            return []
        return list(self.transcript[-limit:])

    def member_names(self) -> list[str]:
        """Insertion-ordered member names — the round-robin order."""
        return list(self.members.keys())

    def snapshot(self) -> dict[str, Any]:
        """Serializable view — for hooks / event log / checkpoint."""
        return {
            "session_id": self.session_id,
            "topic": self.topic,
            "member_count": len(self.members),
            "turn_count": len(self.transcript),
            "round_count": self.round_count(),
            "elapsed_s": time.monotonic() - self.started_at,
        }

    def fingerprint(self) -> tuple[int, str]:
        """Liveness fingerprint — changes whenever a turn is appended; stable
        otherwise. (Ensemble stops via budget/termination, not liveness, but the
        contract requires it and Phase-2 durability uses it as the replay seam.)"""
        last_id = self.transcript[-1].turn_id if self.transcript else ""
        return (len(self.transcript), last_id)
