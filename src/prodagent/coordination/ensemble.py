"""Ensemble — N agents debate on one shared floor (``EnsembleSpec`` / ``ensemble_stream``).

One file, the whole topology:

- **Shared floor** (``SharedFloor``): the append-only transcript every member
  reads and writes — event-sourced when ``event_log`` is attached.
- **Projection** (``FloorProjection``): per-viewer filtering of that transcript
  — the default strips other members' tool calls; the same turn can render
  differently per viewer.
- **Speaking orders** (``RoundRobin`` / ``Moderated`` / ``FreeForAll``): who
  speaks next, adapted to one :class:`~prodagent.ports.activation.Activation`
  per round.
- **Member adapter** (``AgentFloorMember``): turns a full Agent into a
  ``FloorMember`` — each ``speak()`` is one session activation through the
  RunnerPort.
- **Driver + spec + events**: the round loop, its termination, its stream.
"""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from dataclasses import dataclass, field
from enum import StrEnum
from typing import TYPE_CHECKING, Any, Protocol, cast, runtime_checkable

from prodagent.base.codec import dump, load
from prodagent.base.errors import BudgetExceeded
from prodagent.base.text import bound_text
from prodagent.coordination.infra.stage import (
    MaxRounds,
    StageDriver,
    TerminationPolicy,
    TerminationReason,
    ViewInjector,
    has_durable_events,
)
from prodagent.coordination.infra.store import EventSourcedStore, SharedStore
from prodagent.coordination.messaging.envelope import (
    Crossing,
    CrossingKind,
    Direction,
)
from prodagent.coordination.messaging.interceptors import ProjectionInterceptor
from prodagent.coordination.messaging.limits import PUBLIC_TURN_TEXT_MAX_CHARS
from prodagent.coordination.messaging.pipeline import (
    Pipeline,
    Slot,
    admission_pipeline,
    assembly_pipeline,
)
from prodagent.kernel.budget import BudgetLedger, HardBudget, open_ledger
from prodagent.kernel.types import (
    RunCompletedEvent,
    RunFailedEvent,
    RunSuspendedEvent,
    ToolCall,
)
from prodagent.ports.activation import (
    Activation,
    ActivationContext,
    ActivationPolicy,
)
from prodagent.ports.runner import AgentActivation, InProcessChatRunner, RunnerPort

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator, Awaitable, Callable

    from prodagent.base.event_log import Event
    from prodagent.coordination.messaging.pipeline import Interceptor
    from prodagent.kernel.bus import HookRegistry
    from prodagent.kernel.state import AgentRun
    from prodagent.kernel.types import AgentEvent, ToolCall
    from prodagent.ports import EventLog
    from prodagent.ports.dead_letter import DeadLetterStore
    from prodagent.runtime.agent import Agent

logger = logging.getLogger(__name__)

_TURN_TEXT_MAX_CHARS = PUBLIC_TURN_TEXT_MAX_CHARS
"""Admission bound for a floor turn's text — mirrors PublicTextOnly's
per-view cap so the transcript itself (not just its projections) is bounded."""

__all__ = [
    "FloorTurn",
    "FloorMember",
    "SharedFloor",
    "FloorEventType",
    "apply_floor_event",
    "FloorProjection",
    "PublicTextOnly",
    "SelectiveToolExposure",
    "project_floor",
    "EnsembleSpec",
    "Ensemble",
    "AgentFloorMember",
    "RoundRobin",
    "Moderated",
    "FreeForAll",
    "SpeakingOrder",
    "FloorTurnEvent",
    "EnsembleCompletedEvent",
    "ensemble_stream",
]


# ---------------------------------------------------------------------------
# Shared floor — the transcript all members read and write
# ---------------------------------------------------------------------------


class FloorEventType(StrEnum):
    """Durable record of every SharedFloor transition — 1:1 with the in-memory
    mutations. Appended to an :class:`~prodagent.ports.event_log.EventLog` keyed
    by the floor's ``run_id``, so a crashed floor can be rebuilt by
    :meth:`SharedFloor.restore`. Same shape as the work queue's
    ``QueueEventType``."""

    TURN_APPENDED = "TurnAppended"


def apply_floor_event(state: dict[str, Any], event: Event) -> None:
    """Fold one floor :class:`Event` into a rebuild-state dict — the pure
    reducer behind :meth:`SharedFloor.restore`. State shape: ``transcript``
    (list[FloorTurn])."""
    if event.event_type == FloorEventType.TURN_APPENDED:
        state["transcript"].append(FloorTurn.from_dict(event.data["turn"]))


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
    """Spend attributed to this turn — folded into BudgetLedger."""

    tokens: int = 0
    """Token spend attributed to this turn (input + output) — folded into
    BudgetLedger alongside ``cost_usd``. Without it the budget's token axis is
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

    def to_dict(self) -> dict[str, Any]:
        return dump(self)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> FloorTurn:
        return load(
            cls,
            d,
            raw={"turn_id": d.get("turn_id") or str(uuid.uuid4())},
        )


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
class SharedFloor(SharedStore, EventSourcedStore):
    """The shared transcript all ensemble members read and write.

    Lifetime is independent of any single member's run — persists across
    rounds, outliving individual ``AgentRun`` instances the way a chat room
    outlives any one message.
    """

    session_id: str
    """Stable id — correlates to checkpoint / event log / session store."""

    members: dict[str, FloorMember] = field(default_factory=dict)
    """name → member. Insertion order preserved for round-robin. Live
    protocol objects — never serialized; :meth:`SharedFloor.restore`
    re-attaches them."""

    transcript: list[FloorTurn] = field(default_factory=list)
    """All turns, in order. Source of truth for 'what was said'."""

    topic: str = ""
    """The floor's subject — injected into each member's [FLOOR] block."""

    started_at: float = field(default_factory=time.monotonic)
    """Monotonic start — basis for the shared wall-clock budget."""

    event_log: EventLog | None = None
    """Durable projection (optional). When set, every appended turn is also
    an ``Event`` under ``run_id``; the in-memory transcript stays the live
    source during the run, the log is what survives a crash."""

    run_id: str = ""

    def __post_init__(self) -> None:
        self._lock = asyncio.Lock()
        self._last_seq = 0
        # Mixin aliases — the dataclass fields are public (event_log/run_id),
        # EventSourcedStore reads the private spellings.
        self._event_log = self.event_log
        self._run_id = self.run_id
        """Tail of the durable projection — the floor's resume cursor; with a
        log attached it is 1:1 with the mutations and doubles as the durable
        fingerprint (see :meth:`fingerprint`)."""

    def add_member(self, member: FloorMember) -> None:
        if member.name in self.members:
            raise ValueError(
                f"Floor member {member.name!r} already exists on floor "
                f"{self.session_id!r} — names must be unique"
            )
        self.members[member.name] = member

    async def append(self, turn: FloorTurn) -> None:
        """Record a completed turn. Validates speaker membership, not ordering —
        the pipeline sequences."""
        if turn.speaker not in self.members:
            raise ValueError(
                f"Turn speaker {turn.speaker!r} is not a floor member — "
                f"known: {list(self.members.keys())}"
            )
        async with self._lock:
            # Transcript append and durable record share the lock so their
            # orders can't interleave under concurrent appends.
            self.transcript.append(turn)
            await self._record(FloorEventType.TURN_APPENDED, turn=turn.to_dict())

    @classmethod
    async def restore(
        cls,
        event_log: EventLog,
        run_id: str,
        *,
        session_id: str,
        topic: str = "",
        members: list[FloorMember] | None = None,
    ) -> SharedFloor:
        """Rebuild a floor by folding its event log — the crash-recovery path.
        The transcript comes back verbatim (turn ids included); live members
        are re-attached by the caller, same roster as before the crash."""
        events = await event_log.get_events(run_id)
        state: dict[str, Any] = {"transcript": []}
        for event in events:
            apply_floor_event(state, event)
        floor = cls(session_id=session_id, topic=topic, event_log=event_log, run_id=run_id)
        for member in members or []:
            floor.add_member(member)
        floor.transcript = state["transcript"]
        floor._last_seq = events[-1].seq if events else 0
        return floor

    def round_count(self) -> int:
        """Highest round index seen + 1, or 0 if empty. Partial rounds count."""
        if not self.transcript:
            return 0
        return max(t.round for t in self.transcript) + 1

    def recent_turns(self, *, limit: int) -> list[FloorTurn]:
        """Last ``limit`` turns, oldest-first. Caps how much history each
        member sees — mirrors ``prior_output_max_chars`` in the messaging
        plane's :class:`~prodagent.coordination.messaging.packet.HandoffPacket`."""
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
        contract requires it.) With a durable log attached, ``_last_seq`` is the
        same fact on the wire: one seq per mutation, so the resume cursor and
        the fingerprint agree by construction."""
        last_id = self.transcript[-1].turn_id if self.transcript else ""
        return (len(self.transcript), last_id)


# ---------------------------------------------------------------------------
# Floor projection — per-viewer filtering of the transcript
# ---------------------------------------------------------------------------


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

    max_chars: int = PUBLIC_TURN_TEXT_MAX_CHARS
    """Per-turn text cap. Must stay equal to the floor's admission bound
    (``PUBLIC_TURN_TEXT_MAX_CHARS``) — transcript and projection are bounded
    the same, so one long-winded member can't blow another's context window."""

    def project(self, turn: FloorTurn, *, viewer: str) -> FloorTurn:
        # Speaker sees its own turn verbatim — no point truncating your own words.
        if viewer == turn.speaker:
            return turn
        text = bound_text(turn.text, self.max_chars)
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
        text = bound_text(turn.text, self.max_chars)
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


# ---------------------------------------------------------------------------
# Speaking order
# ---------------------------------------------------------------------------


@runtime_checkable
class SpeakingOrder(Protocol):
    """Decides who speaks next. Built-in orders: :class:`RoundRobin` (fixed
    order, looping), :class:`Moderated` (a delegated judge picks — an LLM
    moderator, a scoring rule, anything async), :class:`FreeForAll` (all
    members speak concurrently every round, no arbitration).

    The pipeline adapts whatever it gets to
    :class:`~prodagent.ports.activation.Activation`: an object
    with an async ``pick_speaker`` becomes a serial single-member activation
    per pick (Moderated); an object with ``activation()`` returns batches
    itself (FreeForAll); anything else is the classic sync ``next_speaker``
    protocol (RoundRobin and user orders)."""

    def next_speaker(self, floor: SharedFloor) -> str | None:
        """Return the next member name, or None if the order has no more
        speakers this pass (round-robin never returns None — it loops)."""
        ...


@dataclass
class RoundRobin:
    """Fixed order, looping. ``floor.members`` insertion order is the speaking
    order — deterministic."""

    def next_speaker(self, floor: SharedFloor) -> str | None:
        names = floor.member_names()
        if not names:
            return None
        # Next speaker is the member after the last one, wrapping.
        last = floor.transcript[-1].speaker if floor.transcript else None
        if last is None:
            return names[0]
        idx = names.index(last)
        return names[(idx + 1) % len(names)]


# ---------------------------------------------------------------------------
# Moderated + FreeForAll — the two deferred orders, now real
# ---------------------------------------------------------------------------


@dataclass
class Moderated:
    """A delegated judge picks the next speaker — the AutoGen "selector" shape.

    ``picker`` is any ``async (floor) -> name | None``: an LLM moderator that
    reads the transcript and names the most relevant next speaker, a rule that
    alternates critic/author, a priority function. Returning ``None`` means
    "the moderator concludes the discussion" — the floor stops with a
    ``no_speaker`` termination, the graceful exit round-robin can't express.

    Round semantics: a round is one pass over whoever the moderator selects.
    If the picked speaker already spoke in the current round, a new round
    begins — so ``max_rounds`` still bounds the floor regardless of pick order.
    """

    picker: Callable[[SharedFloor], Awaitable[str | None]]

    async def pick_speaker(self, floor: SharedFloor) -> str | None:
        return await self.picker(floor)

    def round_of(self, floor: SharedFloor, speaker: str) -> int:
        if not floor.transcript:
            return 0
        last = floor.transcript[-1]
        if any(t.speaker == speaker and t.round == last.round for t in floor.transcript):
            return last.round + 1
        return last.round


@dataclass
class FreeForAll:
    """No speaking order — every member speaks *concurrently* every round.

    Runs as one ``dispatch="concurrent"`` activation per round: all members'
    ``speak()`` calls overlap, turns land in member order (deterministic
    stream even though the speaks overlap). Budget honesty: the shared budget
    is checked once before the batch and each member's actuals are committed
    as they finish, but the exhausted re-check happens after the batch — a
    cost-cap overshoot is bounded by one round of concurrent speaks, the same
    shape as Blackboard's concurrent trigger fan-out. If members must not
    duplicate work, that's arbitration — use :class:`Moderated` on the floor
    or Blackboard ``buzz_in`` instead.
    """

    def activation(self, floor: SharedFloor) -> Activation:
        # Every batch is a fresh round: floor.round_count() = max round + 1,
        # which is exactly "the round after everything on the floor so far".
        return Activation(
            members=floor.member_names(),
            dispatch="concurrent",
            round_num=floor.round_count(),
            label="free_for_all",
        )


# ---------------------------------------------------------------------------
# Floor injection (the [FLOOR] block, L2)
# ---------------------------------------------------------------------------


class _FloorViewSlot:
    """Mutable slot holding this member's projected view of the floor.

    Injector closure captures this slot by reference; pipeline writes the
    projected transcript into it before each ``speak()``, injector reads it
    when the context manager calls ``hooks.collect(...)``. Decouples injector
    registration (once, at ensemble start) from per-turn projection (every
    round)."""

    __slots__ = ("view", "topic", "round_num")

    def __init__(self) -> None:
        self.view: list[FloorTurn] = []
        self.topic: str = ""
        self.round_num: int = 0


def _format_floor_block(slot: _FloorViewSlot) -> str:
    """Render the projected transcript as an L2 [FLOOR] snippet. Goes into the
    [MEMORY] block alongside other injected snippets — same L2 layer, same
    compression pipeline. ``[FLOOR]`` prefix lets the member's LLM (and hooks)
    pick it out from [MEMORY] entries."""
    if not slot.view and not slot.topic:
        return ""
    lines: list[str] = [f"[FLOOR] topic: {slot.topic}", f"round: {slot.round_num}"]
    if slot.view:
        lines.append("transcript:")
        for turn in slot.view:
            stamp = f"R{turn.round} {turn.speaker}"
            if turn.addressed_to:
                stamp += f" → {','.join(turn.addressed_to)}"
            if turn.stance:
                stamp += f" [{turn.stance}]"
            lines.append(f"  {stamp}: {turn.text}")
    else:
        lines.append("transcript: (you are first to speak)")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# SpeakingOrder → ActivationPolicy adapters
# ---------------------------------------------------------------------------


class _BatchOrderPolicy:
    """FreeForAll-shaped orders: the order itself returns the activation batch."""

    def __init__(self, order: Any) -> None:
        self._order = order

    async def next_activations(self, ctx: ActivationContext) -> list[Activation]:
        return [self._order.activation(ctx.store)]


class _PickedOrderPolicy:
    """Moderated-shaped orders: an async picker names one speaker at a time."""

    def __init__(self, order: Any) -> None:
        self._order = order

    async def next_activations(self, ctx: ActivationContext) -> list[Activation]:
        speaker = await self._order.pick_speaker(ctx.store)
        if speaker is None:
            return []
        return [
            Activation(
                members=[speaker],
                dispatch="serial",
                round_num=self._order.round_of(ctx.store, speaker),
                label="moderated",
            )
        ]


class _SyncOrderPolicy:
    """Classic ``next_speaker`` orders (RoundRobin and user orders)."""

    def __init__(self, order: Any, compute_round: Any) -> None:
        self._order = order
        self._compute_round = compute_round

    async def next_activations(self, ctx: ActivationContext) -> list[Activation]:
        speaker = self._order.next_speaker(ctx.store)
        if speaker is None:
            return []
        return [
            Activation(
                members=[speaker],
                dispatch="serial",
                round_num=self._compute_round(speaker),
                label=type(self._order).__name__,
            )
        ]


def _order_as_policy(order: Any, compute_round: Any) -> ActivationPolicy:
    """Adapt any speaking-order shape (or a raw policy) to ActivationPolicy."""
    if hasattr(order, "next_activations"):
        return cast("ActivationPolicy", order)
    if hasattr(order, "activation"):
        return _BatchOrderPolicy(order)
    if hasattr(order, "pick_speaker"):
        return _PickedOrderPolicy(order)
    return _SyncOrderPolicy(order, compute_round)


# ---------------------------------------------------------------------------
# AgentFloorMember — adapt a prodagent Agent to the FloorMember protocol
# ---------------------------------------------------------------------------


class AgentFloorMember:
    """Adapts a full :class:`~prodagent.runtime.agent.Agent` to FloorMember.

    Registers a ``[FLOOR]`` injector so the projected transcript lands in L2
    alongside ``[MEMORY]``. Each ``speak()`` updates the injector's view slot,
    activates the member through the :class:`~prodagent.ports.runner.RunnerPort`
    (a session-scoped chat turn — the local default executes it in-process),
    and folds the resulting :class:`AgentRun` into a :class:`FloorTurn`. The
    agent keeps its own ``ConversationSession``, ``MemoryManager``, L0 system
    prompt — personality doesn't bleed across members. The floor is what they
    share; internals stay isolated."""

    def __init__(
        self,
        agent: Agent,
        *,
        session_id: str,
        runner: RunnerPort | None = None,
    ) -> None:
        self._agent = agent
        self._session_id = session_id
        self._runner = runner if runner is not None else InProcessChatRunner()
        self._slot = _FloorViewSlot()
        self._view_injector = ViewInjector(
            agent, block="FLOOR", render=lambda: _format_floor_block(self._slot)
        )
        self._view_pipe: Pipeline | None = None
        self.last_run_id: str = ""
        """Run id of the most recent member activation — set after each
        ``speak()``. Lets callers (e.g. turn-signal collectors) correlate hook
        events back to the floor turn."""

    @property
    def name(self) -> str:
        return self._agent.name

    def _view_pipeline(self, floor: SharedFloor) -> Pipeline:
        """DOWNSTREAM view pipeline: the shared transcript enters this
        member's context through the plane, with the floor projection as the
        AFTER_CONTRACT capability (per-viewer filtering is the floor's trim)."""
        if self._view_pipe is None:
            projection: FloorProjection = getattr(floor, "_projection", PublicTextOnly())
            pipe = assembly_pipeline(hooks=self._agent.hooks)
            pipe.add(Slot.AFTER_CONTRACT, ProjectionInterceptor(self.name, projection))
            self._view_pipe = pipe
        return self._view_pipe

    async def speak(self, floor: SharedFloor, *, round_num: int) -> FloorTurn:
        # The transcript → member-context crossing goes through the plane;
        # what lands in the slot the injector reads is the delivered view.
        delivery = await self._view_pipeline(floor).process(
            Crossing.mint(
                direction=Direction.DOWNSTREAM,
                kind=CrossingKind.DISPATCH,
                from_agent="floor",
                to=self.name,
                payload=list(floor.transcript),
                trace_id=floor.session_id,
            )
        )
        self._slot.view = delivery.crossing.payload
        self._slot.topic = floor.topic
        self._slot.round_num = round_num

        self._view_injector.wire_once()

        # The message the member responds to: the most recent turn addressed
        # to them, or to the floor; else the topic prompt. This is simpler
        # than peer handoff's task_description packing — the floor transcript
        # is already in L2, the message just needs to prompt a response.
        prompt = self._build_prompt(floor)

        run: AgentRun | None = None
        try:
            async for event in self._runner.activate(
                AgentActivation(agent=self._agent, task=prompt, session_id=self._session_id)
            ):
                if isinstance(event, (RunCompletedEvent, RunFailedEvent, RunSuspendedEvent)):
                    run = event.run
        except Exception as exc:  # noqa: BLE001 — a member failing shouldn't kill the floor
            logger.warning(
                "[ensemble] member %s speak() raised %s: %s — emitting pass turn",
                self.name,
                type(exc).__name__,
                exc,
            )
            return FloorTurn(
                speaker=self.name,
                round=round_num,
                text="",
                tool_calls=[],
            )
        if run is None:
            logger.warning(
                "[ensemble] member %s activation ended without a terminal event — "
                "emitting pass turn",
                self.name,
            )
            return FloorTurn(
                speaker=self.name,
                round=round_num,
                text="",
                tool_calls=[],
            )
        self.last_run_id = getattr(run, "run_id", "")

        tool_calls: list[ToolCall] = list(getattr(run, "tool_history", []) or [])
        return FloorTurn(
            speaker=self.name,
            round=round_num,
            text=(run.final_output or "").strip(),
            tool_calls=tool_calls,
            cost_usd=float(getattr(run, "cost_usd", 0.0) or 0.0),
            tokens=int(getattr(run, "input_tokens", 0) or 0)
            + int(getattr(run, "output_tokens", 0) or 0),
            elapsed_s=float(getattr(run, "elapsed_seconds", lambda: 0.0)()),
            turn_id=str(uuid.uuid4()),
        )

    def _build_prompt(self, floor: SharedFloor) -> str:
        """What the member is told to respond to."""
        if not floor.transcript:
            return (
                f"You are the first to speak on this floor. "
                f"Topic: {floor.topic}. Open the conversation."
            )
        last = floor.transcript[-1]
        if last.speaker == self.name:
            return (
                f"You were the last to speak. The floor is still open on "
                f"topic: {floor.topic}. Continue, or pass with an empty reply."
            )
        addressee = " (addressed to you)" if self.name in last.addressed_to else ""
        return f"{last.speaker} just spoke{addressee}. Respond on the floor. Topic: {floor.topic}."


# ---------------------------------------------------------------------------
# EnsembleSpec + pipeline
# ---------------------------------------------------------------------------


@dataclass
class EnsembleSpec:
    """Configuration for an ensemble run.

    ``members`` is a list of :class:`FloorMember` (protocol), not ``Agent`` —
    a hand-rolled adapter qualifies. ``order`` defaults to round-robin;
    ``projection`` defaults to public-text-only (tool calls don't leak).
    ``termination`` is a :class:`TerminationPolicy` — hard cap mandatory,
    business strategy optional.
    """

    members: list[FloorMember]
    topic: str
    name: str = ""
    """Optional handle for the ``run_ensemble`` tool — a spec with a name can
    be declared on ``AgentConfig.ensembles`` and convened by the model."""
    order: SpeakingOrder = field(default_factory=RoundRobin)
    projection: FloorProjection = field(default_factory=PublicTextOnly)
    termination: TerminationPolicy = field(
        default_factory=lambda: TerminationPolicy(hard_cap=MaxRounds(max_rounds=10))
    )
    budget: BudgetLedger | None = None
    """Cross-member ceiling. If None, the pipeline builds one from the members'
    own HardBudget summed (rough) — callers wanting real cost control should
    pass an explicit BudgetLedger."""

    session_id: str = field(default_factory=lambda: str(uuid.uuid4()))

    hooks: HookRegistry | None = None
    """Registry for the speech-admission gate. ``None`` (default) means the
    gate is dormant — mount it by passing the members' shared registry once
    you register ``Gate.AGENT_HANDOFF`` checkers."""

    dead_letter: DeadLetterStore | None = None
    """Where rejected turns land. ``None`` (default) resolves the framework's
    dead-letter backend."""

    admission_interceptors: list[tuple[Slot, Interceptor]] = field(default_factory=list)
    """User-injected semantics on the speech pipeline (injection rules,
    judges, redaction) — mounted at their declared slots, order preserved."""

    event_log: EventLog | None = None
    """Durable projection (optional): every turn appended to the floor also
    lands here under ``run_id``, and a floor with existing events is resumed
    from them instead of starting fresh — same contract as the work queue."""

    run_id: str = ""

    def __post_init__(self) -> None:
        if not self.members:
            raise ValueError("EnsembleSpec.members cannot be empty")
        names = [m.name for m in self.members]
        if len(names) != len(set(names)):
            raise ValueError(f"Ensemble member names must be unique — got: {names}")

    def build_floor(self) -> SharedFloor:
        floor = SharedFloor(
            session_id=self.session_id,
            topic=self.topic,
            event_log=self.event_log,
            run_id=self.run_id,
        )
        for m in self.members:
            floor.add_member(m)
        # Stash the projection on the floor so AgentFloorMember can read it.
        # FloorMember is a protocol — we can't add projection to it, so the
        # pipeline attaches it to the floor as a side-channel: the floor owns
        # "what was said", the projection owns "how to show it".
        floor._projection = self.projection  # type: ignore[attr-defined]
        return floor


# ---------------------------------------------------------------------------
# Events — extend the AgentEvent union with floor-specific ones
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class FloorTurnEvent:
    """Emitted when a member completes a turn on the floor."""

    turn: FloorTurn
    floor_snapshot: dict[str, Any]


@dataclass(frozen=True, slots=True)
class EnsembleCompletedEvent:
    """Emitted when the floor terminates — graceful or hard-capped."""

    reason: TerminationReason
    floor_snapshot: dict[str, Any]
    final_transcript: list[FloorTurn]


# ---------------------------------------------------------------------------
# Pipeline
# ---------------------------------------------------------------------------


class Ensemble(StageDriver[FloorTurnEvent | EnsembleCompletedEvent]):
    """Drives an ensemble: round after round, member after member, until stop.

    One round = one pass of the speaking order. Each member's ``speak()`` is
    awaited in turn (round-robin is inherently serial; concurrent orders are
    deferred). Before each speak, the pipeline checks the BudgetLedger and the
    TerminationPolicy — either can stop the floor. After each speak, actual
    cost is committed to the BudgetLedger.

    The crash→error-event guard and the finalize-to-``unknown`` backstop live
    in :class:`StageDriver`; this class owns only the round loop and the
    terminal event shape."""

    def __init__(self, spec: EnsembleSpec) -> None:
        super().__init__()
        self._spec = spec
        self._activation = _order_as_policy(spec.order, self._compute_round)
        self._floor = spec.build_floor()
        self._opened = False
        # Narrow the base's Optional attribute: Ensemble always has a budget
        # (unlike Blackboard/WorkQueue, where None means unbudgeted).
        self._budget: BudgetLedger = spec.budget or self._build_default_budget()
        # Re-bind spec.budget to the resolved one so callers reading it after
        # the run see actuals.
        spec.budget = self._budget
        # Speech admission (UPSTREAM): a member's turn enters the shared
        # transcript — the one boundary every other member's context reads
        from prodagent.backends.factory import resolve_dead_letter

        # from — through the messaging plane. No contract (FloorTurn is
        # framework-typed); the trim bounds the free text.
        self._speech_pipeline: Pipeline = admission_pipeline(
            trim=self._bound_turn,
            hooks=spec.hooks,
            dead_letter=spec.dead_letter
            if spec.dead_letter is not None
            else resolve_dead_letter(None),
        )
        for slot, interceptor in spec.admission_interceptors:
            self._speech_pipeline.add(slot, interceptor)

    @staticmethod
    def _bound_turn(payload: Any) -> Any:
        """Cap each turn's free text — one long-winded (or poisoned) member
        must not blow every other member's context window."""
        if payload is not None:
            payload.text = bound_text(payload.text, _TURN_TEXT_MAX_CHARS)
        return payload

    def _compute_round(self, speaker: str) -> int:
        """Round index the next ``speaker`` would speak in (round-robin sense).

        Empty floor → round 0. Otherwise look at the last turn: if the next
        speaker comes *before or at* the last speaker in speaking order,
        we've wrapped → new round (last_round + 1). Same position or later →
        still in the last speaker's round."""
        if not self._floor.transcript:
            return 0
        last = self._floor.transcript[-1]
        names = self._floor.member_names()
        if names.index(speaker) <= names.index(last.speaker):
            return last.round + 1
        return last.round

    async def _next_activation(self) -> Activation | None:
        """Ask the activation policy; ``None`` when the order ran out of speakers."""
        activations = await self._activation.next_activations(
            ActivationContext(store=self._floor, round_num=self._floor.round_count())
        )
        return activations[0] if activations else None

    def _build_default_budget(self) -> BudgetLedger:
        """Rough default: sum each member's own HardBudget into a floor cap.

        Deliberately conservative — if no explicit BudgetLedger is passed, the
        floor doesn't run unbounded. Callers wanting real cost control should
        pass an explicit ``BudgetLedger`` tuned to the ensemble (not just the
        sum of per-agent defaults, which can be surprisingly large)."""
        max_turns = 0
        max_seconds = 0.0
        max_tokens = 0
        max_cost = 0.0
        for m in self._spec.members:
            b = getattr(m, "_agent", None)
            if b is not None:
                b = getattr(b, "budget_config", None)
            if isinstance(b, HardBudget):
                max_turns += b.max_turns
                max_seconds += b.max_seconds
                max_tokens += b.max_tokens
                max_cost += b.max_cost_usd
        if max_turns == 0:
            # No member exposed a HardBudget — fall back to a safe default.
            max_turns = self._spec.termination.hard_cap.max_rounds * len(self._spec.members)
            max_seconds = 600.0
            max_tokens = 200_000
            max_cost = 2.0
        ledger = open_ledger(
            HardBudget(
                max_turns=max_turns,
                max_seconds=max_seconds,
                max_tokens=max_tokens,
                max_cost_usd=max_cost,
            )
        )
        assert ledger is not None  # the HardBudget above is always set
        return ledger

    async def _open(self) -> None:
        """Lazy durable setup, run once before the first round. With an event
        log: resume the floor from it when ``run_id`` already has events,
        else start fresh (the first turns record themselves). No-op for
        non-durable floors — same contract as the work queue's ``_open``."""
        if self._opened:
            return
        self._opened = True
        spec = self._spec
        if await has_durable_events(spec):
            assert spec.event_log is not None  # narrowed by has_durable_events
            restored = await SharedFloor.restore(
                spec.event_log,
                spec.run_id,
                session_id=spec.session_id,
                topic=spec.topic,
                members=list(spec.members),
            )
            # Keep the projection side-channel the pipeline stashed earlier.
            restored._projection = self._floor._projection  # type: ignore[attr-defined]
            self._floor = restored

    async def _rounds(self) -> AsyncGenerator[FloorTurnEvent, None]:
        """One activation per iteration: adapt order → check termination/budget
        → dispatch (serial pick or concurrent batch) → append/commit per turn →
        yield. Sets ``self._reason`` and returns when the floor should stop.
        Crash→error and finalize-to-unknown are handled by
        :meth:`StageDriver.run`."""
        await self._open()
        while True:
            # 1. Pick the next activation + the round it belongs in. Done
            #    before termination/budget checks so the policy sees "the
            #    floor is about to enter round N" — max_rounds means "no
            #    member speaks in round N or later".
            activation = await self._next_activation()
            if activation is None:
                self._reason = TerminationReason(
                    reason="no_speaker",
                    detail="Speaking order returned None — floor has no next speaker",
                )
                break
            round_num = activation.round_num

            # 2. Termination check (policy: round cap, business strategy)
            stop, policy_reason = self._spec.termination.should_stop(
                self._floor, next_round=round_num
            )
            if stop and policy_reason is not None:
                self._reason = policy_reason
                break

            # 3. Budget check (hard ceiling, cross-member) — once before the
            #    batch; per-member actuals are committed as they complete and
            #    the exhausted re-check runs after the batch, so a concurrent
            #    batch can overshoot the cap by at most itself.
            try:
                await self._budget.check(member=activation.members[0])
            except BudgetExceeded as exc:
                self._reason = TerminationReason(
                    reason="budget",
                    detail=f"Shared budget exhausted: {exc.message}",
                    by_hard_cap=True,
                )
                break

            # 4. Dispatch — serial (one speaker) or concurrent (FreeForAll).
            results = await self._dispatch(activation, self._speak_member(round_num))

            for _name, turn in results:
                if turn is None:
                    continue  # defensive — speak() always returns a turn
                # Budget first — the spend happened the moment the member
                # spoke, whether or not its speech is admitted to the floor.
                await self._budget.commit(
                    member=turn.speaker,
                    turns=1,
                    tokens=turn.tokens,
                    cost_usd=turn.cost_usd,
                )
                delivery = await self._speech_pipeline.process(
                    Crossing.mint(
                        direction=Direction.UPSTREAM,
                        kind=CrossingKind.SPEECH,
                        from_agent=turn.speaker,
                        to=self._floor.session_id,
                        payload=turn,
                        trace_id=self._floor.session_id,
                        message_id=turn.turn_id,
                        round=round_num,
                    )
                )
                if delivery.status == "duplicate":
                    logger.warning(
                        "[ensemble] turn %s replayed — not appended twice",
                        turn.turn_id[:8],
                    )
                    continue
                if delivery.status == "rejected":
                    # A poisoned turn never enters the shared transcript. A
                    # pass turn keeps the speaking order advancing (RoundRobin
                    # keys off the last speaker) — the floor survives a
                    # poisoned member, mirroring the member-crash path.
                    logger.warning(
                        "[ensemble] turn by %s rejected (%s) — recording pass turn",
                        turn.speaker,
                        delivery.reason[:120],
                    )
                    recorded = FloorTurn(
                        speaker=turn.speaker,
                        round=turn.round,
                        text="",
                        tool_calls=[],
                        turn_id=turn.turn_id,
                    )
                else:
                    recorded = delivery.crossing.payload
                await self._floor.append(recorded)
                yield FloorTurnEvent(turn=recorded, floor_snapshot=self._floor.snapshot())

            # 5. Re-check budget after commit — may have latched exhausted
            if self._budget.is_exhausted():
                self._reason = TerminationReason(
                    reason="budget",
                    detail=f"Shared budget latched exhausted after {activation.why()}",
                    by_hard_cap=True,
                )
                break

    def _speak_member(self, round_num: int) -> Callable[[str], Awaitable[FloorTurn]]:
        """``run_one`` factory for :meth:`StageDriver._dispatch` — speak one
        member. An unknown member name raises KeyError inside, which the
        StageDriver crash guard turns into a terminal error event."""

        async def _speak(speaker: str) -> FloorTurn:
            member = self._floor.members[speaker]
            logger.info(
                "[ensemble] round %d → %s (turn %d)",
                round_num,
                speaker,
                len(self._floor.transcript),
            )
            return await member.speak(self._floor, round_num=round_num)

        return _speak

    def _completed(self, reason: TerminationReason) -> FloorTurnEvent | EnsembleCompletedEvent:
        # The terminal event is the most likely artifact to be logged or
        # persisted wholesale, so it carries a projected digest — safe by
        # construction under either built-in projection. The full transcript
        # stays on the floor for the app that owns this run.
        projection = self._spec.projection
        return EnsembleCompletedEvent(
            reason=reason,
            floor_snapshot=self._floor.snapshot(),
            final_transcript=[
                projection.project(turn, viewer="") for turn in self._floor.transcript
            ],
        )


async def ensemble_stream(
    spec: EnsembleSpec,
) -> AsyncGenerator[AgentEvent | FloorTurnEvent | EnsembleCompletedEvent, None]:
    """Drive an ensemble and stream its events. Top-level entry, parallel to
    :func:`~prodagent.runtime.runner.drive_stream` for single agents."""
    pipeline = Ensemble(spec)
    async for event in pipeline.run():
        yield event
