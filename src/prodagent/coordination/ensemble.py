"""Ensemble — N agents in a shared session, taking turns autonomously."""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Protocol, cast, runtime_checkable

from prodagent.coordination._stage import StageDriver, ViewInjector
from prodagent.coordination.activation import Activation
from prodagent.coordination.budget_ledger import SharedBudget
from prodagent.coordination.floor import FloorMember, FloorTurn, SharedFloor
from prodagent.coordination.floor_projection import (
    FloorProjection,
    PublicTextOnly,
)
from prodagent.coordination.messaging.envelope import (
    Crossing,
    CrossingKind,
    Direction,
)
from prodagent.coordination.messaging.interceptors import ProjectionInterceptor
from prodagent.coordination.messaging.pipeline import (
    Pipeline,
    Slot,
    admission_pipeline,
    assembly_pipeline,
)
from prodagent.coordination.termination import (
    MaxRounds,
    TerminationPolicy,
    TerminationReason,
)
from prodagent.core.budget import HardBudget
from prodagent.core.exceptions import BudgetExceeded

from prodagent.core.text import bound_text

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator, Awaitable, Callable

    from prodagent.coordination.messaging.pipeline import Interceptor
    from prodagent.core.events import AgentEvent
    from prodagent.core.types import ToolCall
    from prodagent.hooks.registry import HookRegistry
    from prodagent.ports.dead_letter import DeadLetterStore
    from prodagent.runtime.agent import Agent

logger = logging.getLogger(__name__)

_TURN_TEXT_MAX_CHARS = 4000
"""Admission bound for a floor turn's text — mirrors PublicTextOnly's
per-view cap so the transcript itself (not just its projections) is bounded."""

__all__ = [
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
# Speaking order
# ---------------------------------------------------------------------------


@runtime_checkable
class SpeakingOrder(Protocol):
    """Decides who speaks next. Built-in orders: :class:`RoundRobin` (fixed
    order, looping), :class:`Moderated` (a delegated judge picks — an LLM
    moderator, a scoring rule, anything async), :class:`FreeForAll` (all
    members speak concurrently every round, no arbitration).

    The pipeline adapts whatever it gets to
    :class:`~prodagent.coordination.activation.Activation`: an object
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
# AgentFloorMember — adapt a prodagent Agent to the FloorMember protocol
# ---------------------------------------------------------------------------


class AgentFloorMember:
    """Adapts a full :class:`~prodagent.runtime.agent.Agent` to FloorMember.

    Registers a ``[FLOOR]`` injector so the projected transcript lands in L2
    alongside ``[MEMORY]``. Each ``speak()`` updates the injector's view slot,
    runs ``agent.chat()``, folds the resulting :class:`AgentRun` into a
    :class:`FloorTurn`. The agent keeps its own ``ConversationSession``,
    ``MemoryManager``, L0 system prompt — personality doesn't bleed across
    members. The floor is what they share; internals stay isolated."""

    def __init__(self, agent: Agent, *, session_id: str) -> None:
        self._agent = agent
        self._session_id = session_id
        self._slot = _FloorViewSlot()
        self._view_injector = ViewInjector(
            agent, block="FLOOR", render=lambda: _format_floor_block(self._slot)
        )
        self._view_pipe: Pipeline | None = None
        self.last_run_id: str = ""
        """Run id of the most recent ``agent.chat()`` call — set after each
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

        try:
            run = await self._agent.chat(prompt, session_id=self._session_id)
            self.last_run_id = getattr(run, "run_id", "")
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
    order: SpeakingOrder = field(default_factory=RoundRobin)
    projection: FloorProjection = field(default_factory=PublicTextOnly)
    termination: TerminationPolicy = field(
        default_factory=lambda: TerminationPolicy(hard_cap=MaxRounds(max_rounds=10))
    )
    budget: SharedBudget | None = None
    """Cross-member ceiling. If None, the pipeline builds one from the members'
    own HardBudget summed (rough) — callers wanting real cost control should
    pass an explicit SharedBudget."""

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

    def __post_init__(self) -> None:
        if not self.members:
            raise ValueError("EnsembleSpec.members cannot be empty")
        names = [m.name for m in self.members]
        if len(names) != len(set(names)):
            raise ValueError(f"Ensemble member names must be unique — got: {names}")

    def build_floor(self) -> SharedFloor:
        floor = SharedFloor(session_id=self.session_id, topic=self.topic)
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
    deferred). Before each speak, the pipeline checks the SharedBudget and the
    TerminationPolicy — either can stop the floor. After each speak, actual
    cost is committed to the SharedBudget.

    The crash→error-event guard and the finalize-to-``unknown`` backstop live
    in :class:`StageDriver`; this class owns only the round loop and the
    terminal event shape."""

    def __init__(self, spec: EnsembleSpec) -> None:
        super().__init__()
        self._spec = spec
        self._floor = spec.build_floor()
        # Narrow the base's Optional attribute: Ensemble always has a budget
        # (unlike Blackboard/WorkQueue, where None means unbudgeted).
        self._budget: SharedBudget = spec.budget or self._build_default_budget()
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
        """Adapt the spec's speaking order to an :class:`Activation`.

        Three shapes, checked in order: ``activation()`` (batch orders —
        FreeForAll), async ``pick_speaker`` (Moderated and user orders shaped
        like it), sync ``next_speaker`` (RoundRobin and user orders shaped
        like it). ``None`` means the order itself ran out of speakers."""
        order = self._spec.order
        if hasattr(order, "activation"):
            batched = cast("FreeForAll", order)  # duck-typed: any batch order qualifies
            return batched.activation(self._floor)
        if hasattr(order, "pick_speaker"):
            moderated = cast("Moderated", order)  # duck-typed: any async picker qualifies
            speaker = await moderated.pick_speaker(self._floor)
            if speaker is None:
                return None
            return Activation(
                members=[speaker],
                dispatch="serial",
                round_num=moderated.round_of(self._floor, speaker),
                label="moderated",
            )
        speaker = order.next_speaker(self._floor)
        if speaker is None:
            return None
        return Activation(
            members=[speaker],
            dispatch="serial",
            round_num=self._compute_round(speaker),
            label=type(order).__name__,
        )

    def _build_default_budget(self) -> SharedBudget:
        """Rough default: sum each member's own HardBudget into a floor cap.

        Deliberately conservative — if no explicit SharedBudget is passed, the
        floor doesn't run unbounded. Callers wanting real cost control should
        pass an explicit ``SharedBudget`` tuned to the ensemble (not just the
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
        return SharedBudget(
            max=HardBudget(
                max_turns=max_turns,
                max_seconds=max_seconds,
                max_tokens=max_tokens,
                max_cost_usd=max_cost,
            )
        )

    async def _rounds(self) -> AsyncGenerator[FloorTurnEvent, None]:
        """One activation per iteration: adapt order → check termination/budget
        → dispatch (serial pick or concurrent batch) → append/commit per turn →
        yield. Sets ``self._reason`` and returns when the floor should stop.
        Crash→error and finalize-to-unknown are handled by
        :meth:`StageDriver.run`."""
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
                self._floor.append(recorded)
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
    :func:`~prodagent.coordination.run_loop.drive_stream` for single agents."""
    pipeline = Ensemble(spec)
    async for event in pipeline.run():
        yield event
