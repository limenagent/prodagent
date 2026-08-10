"""Multi-agent coordination UI infrastructure — unified event protocol + adapter driver.

The playground used to special-case ``dating_chat`` with a dedicated route pair
and a ``datingMode`` frontend branch. That doesn't scale: ``quiz_arena`` runs
WorkQueue then Blackboard, future examples will add more primitive combinations.
This module is the generic layer every multi-agent example plugs into.

The split:

- **Adapter** (per-example) — owns the primitive's ``stream()`` coroutine and a
  ``map_event`` that converts each primitive event (``FloorTurnEvent``,
  ``BoardWriteEvent``, ``ItemClaimedEvent``, …) into one normalized
  :class:`MultiAgentEvent` envelope. Stateful: tracks current phase, validated
  items, participant states, whatever the example needs.
- **Driver** (:class:`MultiAgentRun`) — primitive-agnostic. Pumps
  ``adapter.stream()`` through ``adapter.map_event()`` onto an ``asyncio.Queue``
  that the SSE route reads. Catches adapter crashes and emits a terminal
  ``failed`` envelope.

The envelope carries ``actor`` (who did it) and ``phase`` (which segment of a
multi-phase run) but **not** the full participant roster — roster updates travel
as their own ``kind="roster"`` events so adapters don't have to re-derive the
full roster on every primitive event (``BoardWriteEvent`` doesn't carry the
losing buzz_in candidates, so forcing roster-on-every-event would be brittle).
"""

from __future__ import annotations

import asyncio
import dataclasses
import enum
import logging
import time
from dataclasses import dataclass, field
from pathlib import PurePath
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator

logger = logging.getLogger(__name__)


__all__ = [
    "ParticipantStatus",
    "MultiAgentEvent",
    "MultiAgentAdapter",
    "PhaseStarted",
    "PhaseCompleted",
    "MultiAgentRun",
    "event_to_dict",
]


# ---------------------------------------------------------------------------
# Envelope — every primitive event gets normalized into this shape
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ParticipantStatus:
    """One row in the left roster panel."""

    name: str
    role: str
    """``speaker`` / ``expert`` / ``worker`` / ``host`` / ``trigger``."""

    state: str
    """``idle`` / ``computing`` / ``locked_winner`` / ``locked_loser`` / ``failed`` / ``completed``."""

    meta: dict[str, Any] = field(default_factory=dict)
    """Primitive-specific extras (trigger_name, item_id, attempts, …)."""


@dataclass(frozen=True, slots=True)
class MultiAgentEvent:
    """The normalized envelope the frontend renders.

    The frontend's ``renderMultiAgentEvent(envelope)`` dispatches on :attr:`kind`:
    ``turn`` → chat bubble, ``write`` → board-write card, ``claim``/``complete``/
    ``requeue``/``dead_letter`` → worker cards, ``phase_started``/``phase_completed``
    → phase divider, ``roster`` → update left panel (no center render),
    ``started``/``completed``/``failed`` → terminal cards.
    """

    kind: str
    actor: str | None
    """Who did it; ``None`` for system-level events (phase markers, roster, terminal)."""

    phase: str | None
    """Which segment of a multi-phase run; ``None`` for single-phase examples.

    ``quiz_arena`` uses ``"backstage_review"`` (WorkQueue) and ``"live_quiz"``
    (Blackboard); ``dating_chat`` leaves it ``None``.
    """

    summary: dict[str, Any]
    """Structured one-liner: ``{"verb": "spoke", "object": "…"}``. Frontend formats."""

    payload: dict[str, Any]
    """Full structured detail for the expandable card body."""

    snapshot: dict[str, Any]
    """The primitive's own ``*_snapshot`` (``floor_snapshot`` / ``board_snapshot`` /
    ``queue_snapshot``), passed through unchanged. Right panel renders the latest."""

    timestamp: float = field(default_factory=time.time)


# ---------------------------------------------------------------------------
# Phase sentinels — adapter yields these between primitive streams
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class PhaseStarted:
    """Adapter-internal sentinel: a new phase is starting."""

    phase: str
    detail: str = ""


@dataclass(frozen=True, slots=True)
class PhaseCompleted:
    """Adapter-internal sentinel: a phase finished, next one may start."""

    phase: str
    detail: str = ""
    counts: dict[str, int] = field(default_factory=dict)
    """Optional tallies (e.g. ``{"validated": 3}``) the frontend can show on the divider."""


# ---------------------------------------------------------------------------
# Adapter protocol — each example provides one
# ---------------------------------------------------------------------------


@runtime_checkable
class MultiAgentAdapter(Protocol):
    """Owns the primitive's ``stream()`` and maps its events to the envelope.

    The adapter is stateful (current phase, validated items, participant roster,
    side-channel accumulators). Only one :class:`MultiAgentRun` should drive an
    adapter instance — :meth:`MultiAgentRun.__init__` asserts this.
    """

    name: str

    def initial_participants(self) -> list[ParticipantStatus]:
        """Roster at run start. Emitted as the first ``kind="roster"`` event."""
        ...

    def map_event(self, event: Any) -> MultiAgentEvent | list[MultiAgentEvent]:
        """Convert one primitive event (or :class:`PhaseStarted` / :class:`PhaseCompleted`
        sentinel) into an envelope. May return a list to fan out into several
        envelopes (e.g. a buzz_in ``BoardWriteEvent`` that first emits a roster
        update marking winner/loser, then the write itself).

        Sync — pure transformation over frozen dataclasses, no I/O.
        """
        ...

    async def stream(self) -> AsyncGenerator[Any, None]:
        """Yield primitive events and phase sentinels. The adapter owns the
        primitive's construction (``EnsembleSpec`` / ``BlackboardSpec`` /
        ``WorkQueueSpec``) and any cross-primitive sequencing."""
        ...


# ---------------------------------------------------------------------------
# Driver — pumps adapter.stream() → adapter.map_event() → queue
# ---------------------------------------------------------------------------


class MultiAgentRun:
    """Drives one adapter instance, pushes envelopes onto a queue for SSE.

    Ephemeral: unlike single-agent runs (which have checkpoint reconstruction),
    multi-agent runs live only in this process. If the browser disconnects the
    run continues; the client loses the stream. A ring-buffer replay is a v2
    concern.
    """

    def __init__(self, adapter: MultiAgentAdapter, *, run_id: str) -> None:
        if getattr(adapter, "_attached_run", None) is not None:
            raise RuntimeError(
                f"adapter {adapter.name!r} is already attached to run "
                f"{adapter._attached_run!r} — create a fresh adapter per run"
            )
        self.adapter = adapter
        self.run_id = run_id
        self.queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        self._task: asyncio.Task[None] | None = None
        self._terminal = False
        adapter._attached_run = run_id  # type: ignore[attr-defined]

    @property
    def terminal(self) -> bool:
        return self._terminal

    def start(self) -> None:
        if self._task is not None:
            raise RuntimeError(f"run {self.run_id} already started")
        self._task = asyncio.create_task(self._drive(), name=f"multiagent:{self.run_id}")

    async def _drive(self) -> None:
        # Seed the roster before any primitive event fires.
        await self._emit_roster(self.adapter.initial_participants(), phase=None)
        await self._emit(
            MultiAgentEvent(
                kind="started",
                actor=None,
                phase=None,
                summary={"verb": "started", "object": self.adapter.name},
                payload={},
                snapshot={},
            )
        )
        try:
            async for primitive_event in self.adapter.stream():
                mapped = self.adapter.map_event(primitive_event)
                if isinstance(mapped, MultiAgentEvent):
                    await self._emit(mapped)
                else:
                    for envelope in mapped:
                        await self._emit(envelope)
        except Exception as exc:
            logger.exception("[multiagent] %s crashed", self.run_id)
            self._terminal = True
            await self._emit(
                MultiAgentEvent(
                    kind="failed",
                    actor=None,
                    phase=None,
                    summary={"verb": "failed", "object": self.adapter.name},
                    payload={"error": f"{type(exc).__name__}: {exc}"},
                    snapshot={},
                )
            )
            return

        self._terminal = True
        await self._emit(
            MultiAgentEvent(
                kind="completed",
                actor=None,
                phase=None,
                summary={"verb": "completed", "object": self.adapter.name},
                payload={},
                snapshot={},
            )
        )

    async def _emit(self, envelope: MultiAgentEvent) -> None:
        await self.queue.put(event_to_dict(envelope))

    async def _emit_roster(
        self, participants: list[ParticipantStatus], *, phase: str | None
    ) -> None:
        await self._emit(
            MultiAgentEvent(
                kind="roster",
                actor=None,
                phase=phase,
                summary={"verb": "roster", "object": f"{len(participants)} participants"},
                payload={
                    "participants": [_participant_to_dict(p) for p in participants],
                },
                snapshot={},
            )
        )

    async def aclose(self) -> None:
        if self._task is not None and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass


# ---------------------------------------------------------------------------
# JSON serialization — mirrors web_hooks._jsonable but for the envelope
# ---------------------------------------------------------------------------


def event_to_dict(envelope: MultiAgentEvent) -> dict[str, Any]:
    return {
        "type": "event",
        "kind": envelope.kind,
        "actor": envelope.actor,
        "phase": envelope.phase,
        "summary": _jsonable(envelope.summary),
        "payload": _jsonable(envelope.payload),
        "snapshot": _jsonable(envelope.snapshot),
        "timestamp": envelope.timestamp,
    }


def _participant_to_dict(p: ParticipantStatus) -> dict[str, Any]:
    return {
        "name": p.name,
        "role": p.role,
        "state": p.state,
        "meta": _jsonable(p.meta),
    }


def _jsonable(obj: Any) -> Any:
    """Recursively coerce *obj* to JSON-serializable primitives."""
    if obj is None or isinstance(obj, bool | int | float | str):
        return obj
    if isinstance(obj, enum.Enum):
        return obj.value
    if isinstance(obj, PurePath):
        return str(obj)
    if isinstance(obj, dict):
        return {str(k): _jsonable(v) for k, v in obj.items()}
    if isinstance(obj, list | tuple | set | frozenset):
        return [_jsonable(v) for v in obj]
    if dataclasses.is_dataclass(obj) and not isinstance(obj, type):
        return {f.name: _jsonable(getattr(obj, f.name)) for f in dataclasses.fields(obj)}
    if hasattr(obj, "model_dump") and callable(obj.model_dump):
        try:
            return _jsonable(obj.model_dump(mode="json"))
        except Exception:
            logger.warning(
                "[multiagent] model_dump() failed for %r; falling back to repr",
                type(obj).__name__,
            )
    return repr(obj)
