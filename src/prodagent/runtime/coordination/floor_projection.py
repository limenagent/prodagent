"""FloorProjection — per-viewer filtering of the shared transcript.

A :class:`~prodagent.runtime.coordination.floor.SharedFloor` is the single
source of truth for what was said, but not every member should see every byte.
A member may have private tools (read its own memory store, query an internal
DB) whose results shouldn't appear in another member's view of the transcript
— that's a capability-leak. And in long debates, capping how much history each
member sees mirrors the ``prior_output_max_chars`` truncation in
:class:`~prodagent.runtime.coordination.handoff.HandoffPacket`.

Conceptually this is the same move as
:class:`~prodagent.runtime.coordination.handoff.HandoffInterceptor` — both
filter what crosses an agent boundary — but the mechanism is different:

- ``HandoffInterceptor`` filters a *dict* by field whitelist, one global rule
  per spawn. It runs once, at handoff time, on a one-shot packet.
- ``FloorProjection`` filters a *list of structured turns*, per-viewer, on
  every member's ``speak()`` call. The same turn can produce different views
  for different viewers.

The two share an idea (filtering at the boundary) but not code. The
``intercept(result, contract)`` signature style is mirrored here as
``project(turn, viewer)`` so the family reads consistently.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Protocol, runtime_checkable

from prodagent.runtime.coordination.floor import FloorTurn

if TYPE_CHECKING:
    from prodagent.runtime.coordination.floor import SharedFloor

__all__ = [
    "FloorProjection",
    "PublicTextOnly",
    "SelectiveToolExposure",
    "project_floor",
]


@runtime_checkable
class FloorProjection(Protocol):
    """Per-viewer filter applied to each transcript turn before a member sees it.

    Called once per turn per viewer by the pipeline. Implementations must be
    pure — no mutation of the input turn. Return a new :class:`FloorTurn`
    (or the same one unchanged) reflecting what ``viewer`` should see.
    """

    def project(self, turn: FloorTurn, *, viewer: str) -> FloorTurn: ...


@dataclass
class PublicTextOnly:
    """Default projection — only the utterance text crosses the boundary.

    ``tool_calls`` are stripped entirely, ``stance``/``addressed_to`` are
    preserved (they're cheap metadata and useful for a moderator to reason
    about). ``cost_usd``/``elapsed_s`` are zeroed — they're internal metrics,
    not part of the conversation. This is the safe default: a member's private
    tool results never appear in another member's view.
    """

    max_chars: int = 4000
    """Per-turn text cap. Mirrors HandoffPacket.prior_output_max_chars — one
    long-winded member shouldn't blow another member's context window."""

    def project(self, turn: FloorTurn, *, viewer: str) -> FloorTurn:
        # The speaker always sees its own turn verbatim — no point truncating
        # your own words back at you.
        if viewer == turn.speaker:
            return turn
        text = turn.text
        if len(text) > self.max_chars:
            text = text[: self.max_chars] + (
                f"\n…(truncated, {len(turn.text) - self.max_chars} more chars)"
            )
        return FloorTurn(
            speaker=turn.speaker,
            round=turn.round,
            text=text,
            addressed_to=list(turn.addressed_to),
            stance=turn.stance,
            tool_calls=[],
            cost_usd=0.0,
            elapsed_s=0.0,
            turn_id=turn.turn_id,
            created_at=turn.created_at,
        )


@dataclass
class SelectiveToolExposure:
    """Whitelist which tool calls each viewer may see.

    ``tool_visibility`` maps ``tool_name`` → list of viewer names allowed to
    see it. Tools absent from the map are hidden from everyone (default-deny).
    Use this when a member has tools whose results are fine to share with some
    peers but not others — e.g. a research agent's ``web_fetch`` results are
    fine for the debate judge to see, but its ``read_private_notes`` is not.
    """

    tool_visibility: dict[str, list[str]] = field(default_factory=dict)
    max_chars: int = 4000

    def project(self, turn: FloorTurn, *, viewer: str) -> FloorTurn:
        if viewer == turn.speaker:
            return turn
        text = turn.text
        if len(text) > self.max_chars:
            text = text[: self.max_chars] + (
                f"\n…(truncated, {len(turn.text) - self.max_chars} more chars)"
            )
        allowed = [
            call for call in turn.tool_calls if viewer in self.tool_visibility.get(call.name, [])
        ]
        return FloorTurn(
            speaker=turn.speaker,
            round=turn.round,
            text=text,
            addressed_to=list(turn.addressed_to),
            stance=turn.stance,
            tool_calls=allowed,
            cost_usd=0.0,
            elapsed_s=0.0,
            turn_id=turn.turn_id,
            created_at=turn.created_at,
        )


def project_floor(
    floor: SharedFloor,
    *,
    viewer: str,
    projection: FloorProjection,
    limit: int = 0,
) -> list[FloorTurn]:
    """Project the floor's transcript for ``viewer``.

    ``limit`` caps how many recent turns to include (0 = no cap). Apply this
    per-viewer right before handing the transcript to a member's ``speak()`` —
    it's the multi-turn analogue of HandoffPacket's single-shot prior_output
    truncation, generalized to N viewers and a growing transcript.
    """
    turns = floor.recent_turns(limit=limit) if limit > 0 else list(floor.transcript)
    return [projection.project(t, viewer=viewer) for t in turns]
