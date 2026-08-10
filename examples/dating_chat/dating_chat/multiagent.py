"""Multi-agent adapter for dating_chat — maps ``run_conversation()``'s ``Line``
objects into the unified :class:`MultiAgentEvent` envelope.

The playground's generic multi-agent UI renders this as a stream of
``kind="turn"`` events — chat bubbles in the center column. The bubble aesthetic
is a *rendering strategy* for ``kind="turn"``, not a separate mode: dating_chat
and any future turn-based example share the same envelope and the same
frontend renderer.

This adapter replaces the former ``web.py`` (which pushed a custom dict shape
through a dedicated ``/api/dating_chat/*`` route pair). The orchestrator's
``run_conversation()`` is reused unchanged — it already enriches each
``FloorTurnEvent`` with hook-derived signals (memory_hits, compression,
niu_note, tool_calls) into a ``Line``. The adapter is a thin mapper on top.
"""

from __future__ import annotations

import time
from typing import Any

from prodagent.playground.multiagent import (
    MultiAgentAdapter,
    MultiAgentEvent,
    ParticipantStatus,
)

from dating_chat.orchestrator import Line, run_conversation


class DatingChatAdapter:
    """Wraps ``run_conversation()`` — yields ``Line`` objects, maps each to a
    ``kind="turn"`` envelope. Single-phase (``phase=None`` throughout)."""

    name = "dating_chat"

    def __init__(self, *, session_id: str = "") -> None:
        self._session_id = session_id or f"dating-chat-{int(time.time() * 1000) & 0xFFFFFF:x}"
        self._niu_state = "idle"
        self._mei_state = "idle"

    def initial_participants(self) -> list[ParticipantStatus]:
        return [
            ParticipantStatus(name="大牛", role="speaker", state="idle", meta={}),
            ParticipantStatus(name="小美", role="speaker", state="idle", meta={}),
        ]

    def map_event(self, event: Any) -> MultiAgentEvent:
        if not isinstance(event, Line):
            raise TypeError(f"DatingChatAdapter.map_event expects Line, got {type(event).__name__}")

        is_niu = event.speaker == "大牛"
        actor = event.speaker
        # Flip the speaker's state to "computing" briefly — the frontend reads
        # this off the turn payload rather than a separate roster event, since
        # ensemble is a serial round-robin (no concurrent speakers to disambiguate).
        return MultiAgentEvent(
            kind="turn",
            actor=actor,
            phase=None,
            summary={"verb": "spoke", "object": event.text[:60]},
            payload={
                "speaker": event.speaker,
                "text": event.text,
                "round": event.round,
                "memory_hits": event.memory_hits,
                "memory_previews": list(event.memory_previews),
                "compression": event.compression,
                "history_summary": event.history_summary,
                "tool_compress_sample": event.tool_compress_sample,
                "tool_calls": list(event.tool_calls),
                "niu_note": event.niu_note,
                "is_niu": is_niu,
            },
            snapshot=event.floor_snapshot,
        )

    async def stream(self):
        async for line in run_conversation(session_id=self._session_id):
            yield line


def build_adapter() -> MultiAgentAdapter:
    """Factory called by :func:`discover_examples` — return a fresh adapter per run."""
    return DatingChatAdapter()


__all__ = ["DatingChatAdapter", "build_adapter"]
