"""FloorProjection — per-viewer filtering of the shared transcript.

:class:`~prodagent.runtime.coordination.floor.SharedFloor` is the single source
of truth for what was said, but not every member should see every byte — a
member's private tool results are a capability-leak if they reach another
member's view. Same move as
:class:`~prodagent.runtime.coordination.handoff.HandoffInterceptor` (filter
what crosses an agent boundary) but different mechanism: ``HandoffInterceptor``
filters a one-shot dict once at handoff; ``FloorProjection`` filters a
growing list of structured turns per-viewer, every ``speak()``. The same turn
can produce different views for different viewers.
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

    Called once per turn per viewer by the pipeline. Must be pure — no mutation
    of the input turn. Return a new :class:`FloorTurn` reflecting what
    ``viewer`` should see.
    """

    def project(self, turn: FloorTurn, *, viewer: str) -> FloorTurn: ...


@dataclass
class PublicTextOnly:
    """Default projection — only the utterance text crosses the boundary.

    ``tool_calls`` stripped entirely; ``stance``/``addressed_to`` preserved
    (cheap metadata, useful for a moderator); ``cost_usd``/``elapsed_s``
    zeroed (internal metrics). Safe default: a member's private tool results
    never appear in another member's view.
    """

    max_chars: int = 4000
    """Per-turn text cap. Mirrors HandoffPacket.prior_output_max_chars — one
    long-winded member shouldn't blow another's context window."""

    def project(self, turn: FloorTurn, *, viewer: str) -> FloorTurn:
        # Speaker sees its own turn verbatim — no point truncating your own words.
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
    Use when a member has tools shareable with some peers but not others —
    e.g. a research agent's ``web_fetch`` is fine for the judge to see, its
    ``read_private_notes`` is not.
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
    """Project the floor's transcript for ``viewer``. ``limit`` caps recent
    turns (0 = no cap). Apply per-viewer right before handing the transcript
    to ``speak()`` — multi-turn analogue of HandoffPacket's single-shot
    prior_output truncation, generalized to N viewers."""
    turns = floor.recent_turns(limit=limit) if limit > 0 else list(floor.transcript)
    return [projection.project(t, viewer=viewer) for t in turns]
