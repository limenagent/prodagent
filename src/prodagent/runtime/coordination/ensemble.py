"""EnsemblePipeline — N agents in a shared session, taking turns autonomously.

The third coordination primitive, alongside :class:`SpawnPipeline`
(``agents=`` — vertical delegation, parent waits for child) and
:class:`PeerPipeline` (``peers=`` — linear baton-pass, each prior run
terminated). Where those two are single-shot and directed, an ensemble is
*co-present*: every member reads the same :class:`SharedFloor` transcript,
each writes its own turns, nobody "leaves". This is the substrate for debate,
conversation, and role-play.

Minimal closed loop: :class:`RoundRobin` order + :class:`MaxRounds` hard cap
+ :class:`SharedBudget` cross-member ceiling + :class:`FloorProjection`
per-viewer filtering. The :class:`Moderated` / consensus / convergence
strategies and the ``FreeForAll`` order are deferred — see tasks #5 and the
design discussion.

Membership is the :class:`FloorMember` protocol, not ``Agent``. A hand-rolled
``messages`` list wrapped in an adapter qualifies — that's the point: the
floor treats all members equally, whether they remember things is their own
business. :class:`AgentFloorMember` adapts a full prodagent ``Agent``;
:class:`FloorMember` is satisfied by anything with ``name`` + async
``speak()``.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

from prodagent.core.budget import HardBudget
from prodagent.core.exceptions import BudgetExceeded
from prodagent.runtime.coordination.ensemble_budget import SharedBudget
from prodagent.runtime.coordination.floor import FloorMember, FloorTurn, SharedFloor
from prodagent.runtime.coordination.floor_projection import (
    FloorProjection,
    PublicTextOnly,
    project_floor,
)
from prodagent.runtime.coordination.termination import (
    MaxRounds,
    TerminationPolicy,
    TerminationReason,
)

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator

    from prodagent.core.events import AgentEvent
    from prodagent.core.types import ToolCall
    from prodagent.runtime.agent import Agent

logger = logging.getLogger(__name__)

__all__ = [
    "EnsembleSpec",
    "EnsemblePipeline",
    "AgentFloorMember",
    "RoundRobin",
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
    """Decides who speaks next, given the floor's state.

    The minimal closed loop implements only :class:`RoundRobin`.
    :class:`Moderated` (a judge agent picks) and ``FreeForAll`` (first
    ready-wins, locked) are deferred.
    """

    def next_speaker(self, floor: SharedFloor) -> str | None:
        """Return the next member name, or None if the order has no more
        speakers this pass (round-robin never returns None between rounds —
        it loops)."""
        ...


@dataclass
class RoundRobin:
    """Fixed order, looping. The insertion order of floor.members is the
    speaking order — deterministic and easy to reason about."""

    def next_speaker(self, floor: SharedFloor) -> str | None:
        names = floor.member_names()
        if not names:
            return None
        # Who spoke last? The next speaker is the member after them, wrapping.
        last = floor.transcript[-1].speaker if floor.transcript else None
        if last is None:
            return names[0]
        idx = names.index(last)
        return names[(idx + 1) % len(names)]


# ---------------------------------------------------------------------------
# Floor injection (the [FLOOR] block, L2)
# ---------------------------------------------------------------------------


class _FloorViewSlot:
    """Mutable slot holding this member's projected view of the floor.

    The injector closure captures this slot by reference; the pipeline writes
    the projected transcript into it before each ``speak()``. The injector
    reads it when the context manager calls ``hooks.collect(...)`` during
    context assembly. This decouples the injector registration (once, at
    ensemble start) from the per-turn projection (every round).
    """

    __slots__ = ("view", "topic", "round_num")

    def __init__(self) -> None:
        self.view: list[FloorTurn] = []
        self.topic: str = ""
        self.round_num: int = 0


def _format_floor_block(slot: _FloorViewSlot) -> str:
    """Render the projected transcript as an L2 [FLOOR] snippet.

    Goes into the [MEMORY] block alongside other injected snippets — same
    L2 layer, same compression pipeline. The ``[FLOOR]`` prefix lets the
    member's LLM (and hooks) pick it out from [MEMORY] entries.
    """
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


def _make_floor_injector(slot: _FloorViewSlot) -> Any:
    """Build an async injector closure bound to ``slot``.

    The injector is registered at ``InjectionPoint.CONTEXT_INJECTOR`` — same
    point MemoryHooks uses. It returns the [FLOOR] snippet string (or empty
    string, which the context manager filters out). ``query`` is ignored: the
    floor view is set externally by the pipeline, not derived from the task.
    """

    async def _injector(**kw: Any) -> str:
        return _format_floor_block(slot)

    return _injector


# ---------------------------------------------------------------------------
# AgentFloorMember — adapt a prodagent Agent to the FloorMember protocol
# ---------------------------------------------------------------------------


class AgentFloorMember:
    """Adapts a full :class:`~prodagent.runtime.agent.Agent` to FloorMember.

    Registers a ``[FLOOR]`` injector on the agent's hook registry so the
    projected transcript lands in L2 alongside ``[MEMORY]``. Each ``speak()``
    call updates the injector's view slot, then runs ``agent.chat()`` and
    folds the resulting :class:`AgentRun` into a :class:`FloorTurn`.

    The agent keeps its own ``ConversationSession``, ``MemoryManager``, and
    L0 system prompt — personality doesn't bleed across members. The floor is
    what they share; their internals stay isolated.
    """

    def __init__(self, agent: Agent, *, session_id: str) -> None:
        self._agent = agent
        self._session_id = session_id
        self._slot = _FloorViewSlot()
        self._injector_wired = False
        self.last_run_id: str = ""
        """Run id of the most recent ``agent.chat()`` call — set after each
        ``speak()``. Lets callers (e.g. turn-signal collectors) correlate
        hook events back to the floor turn."""

    @property
    def name(self) -> str:
        return self._agent.name

    async def speak(self, floor: SharedFloor, *, round_num: int) -> FloorTurn:
        # Project the floor for this viewer, stash in the slot the injector reads.
        projection: FloorProjection = getattr(floor, "_projection", PublicTextOnly())
        self._slot.view = project_floor(floor, viewer=self.name, projection=projection)
        self._slot.topic = floor.topic
        self._slot.round_num = round_num

        self._wire_floor_injector_once()

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
            elapsed_s=float(getattr(run, "elapsed_seconds", lambda: 0.0)()),
            turn_id=str(uuid.uuid4()),
        )

    def _wire_floor_injector_once(self) -> None:
        if self._injector_wired:
            return
        from prodagent.hooks.checkpoint import InjectionPoint

        hooks = self._agent.hooks
        if hooks is None:
            # attach_default_hooks is normally called lazily during drive_stream,
            # but we need the registry now to register the injector before the
            # first chat() call resolves hooks.
            hooks = self._agent.attach_default_hooks()
        if hooks is None:
            logger.warning(
                "[ensemble] agent %s has no hooks registry — [FLOOR] block "
                "will not be injected; member won't see other members' turns",
                self.name,
            )
            return
        hooks.register_injector(InjectionPoint.CONTEXT_INJECTOR, _make_floor_injector(self._slot))
        self._injector_wired = True

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
    ``termination`` is a :class:`TerminationPolicy` — the hard cap is
    mandatory, the business strategy is optional.
    """

    members: list[FloorMember]
    topic: str
    order: SpeakingOrder = field(default_factory=RoundRobin)
    projection: FloorProjection = field(default_factory=PublicTextOnly)
    termination: TerminationPolicy = field(
        default_factory=lambda: TerminationPolicy(hard_cap=MaxRounds(max_rounds=10))
    )
    budget: SharedBudget | None = None
    """Cross-member ceiling. If None, the pipeline builds one from the
    members' own HardBudget summed (rough) — but callers wanting real cost
    control should pass an explicit SharedBudget."""

    session_id: str = field(default_factory=lambda: str(uuid.uuid4()))

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
        # (FloorMember is a protocol — we can't add projection to it, so the
        # pipeline attaches it to the floor as a side-channel. This is the
        # least-bad option: the floor owns "what was said", the projection
        # owns "how to show it", and members read both.)
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


class EnsemblePipeline:
    """Drives an ensemble: round after round, member after member, until stop.

    One round = one pass of the speaking order. Each member's ``speak()`` is
    awaited in turn (round-robin is inherently serial; concurrent orders are
    deferred). Before each speak, the pipeline checks the SharedBudget and
    the TerminationPolicy — either can stop the floor. After each speak, the
    actual cost is committed to the SharedBudget.
    """

    def __init__(self, spec: EnsembleSpec) -> None:
        self._spec = spec
        self._floor = spec.build_floor()
        self._budget = spec.budget or self._build_default_budget()
        # Re-bind the spec's budget to the resolved one so callers reading
        # spec.budget after the run see actuals.
        spec.budget = self._budget

    def _compute_round(self, speaker: str) -> int:
        """Round index the next ``speaker`` would speak in.

        Empty floor → round 0. Otherwise, look at the last turn: if the next
        speaker comes *before or at* the last speaker in speaking order,
        we've wrapped around → new round (last_round + 1). Same position or
        later in the order → still in the last speaker's round.
        """
        if not self._floor.transcript:
            return 0
        last = self._floor.transcript[-1]
        names = self._floor.member_names()
        if names.index(speaker) <= names.index(last.speaker):
            return last.round + 1
        return last.round

    def _build_default_budget(self) -> SharedBudget:
        """Rough default: sum each member's own HardBudget into a floor cap.

        This is deliberately conservative — if no explicit SharedBudget is
        passed, we don't let the floor run unbounded. But callers wanting real
        cost control should pass an explicit ``SharedBudget`` with caps tuned
        to the ensemble (not just the sum of per-agent defaults, which can be
        surprisingly large).
        """
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

    async def run(
        self,
    ) -> AsyncGenerator[AgentEvent | FloorTurnEvent | EnsembleCompletedEvent, None]:
        """Stream floor events. Yields FloorTurnEvent per turn, EnsembleCompletedEvent at end."""
        reason: TerminationReason | None = None
        try:
            while True:
                # 1. Pick next speaker + compute the round they'd speak in.
                #    Done before termination/budget checks so the policy sees
                #    "the floor is about to enter round N" — letting max_rounds
                #    mean "no member speaks in round N or later" (max_rounds=2
                #    → 2 × N turns for N members, a clean mental model).
                speaker = self._spec.order.next_speaker(self._floor)
                if speaker is None:
                    reason = TerminationReason(
                        reason="no_speaker",
                        detail="Speaking order returned None — floor has no next speaker",
                    )
                    break
                round_num = self._compute_round(speaker)

                # 2. Termination check (policy level — round cap, business strategy)
                stop, policy_reason = self._spec.termination.should_stop(
                    self._floor, next_round=round_num
                )
                if stop and policy_reason is not None:
                    reason = policy_reason
                    break

                # 3. Budget check (hard ceiling — cross-member)
                try:
                    await self._budget.check(member=speaker)
                except BudgetExceeded as exc:
                    reason = TerminationReason(
                        reason="budget",
                        detail=f"Shared budget exhausted: {exc.message}",
                        by_hard_cap=True,
                    )
                    break

                # 4. Speak
                member = self._floor.members[speaker]
                logger.info(
                    "[ensemble] round %d → %s (turn %d)",
                    round_num,
                    speaker,
                    len(self._floor.transcript),
                )

                turn = await member.speak(self._floor, round_num=round_num)
                self._floor.append(turn)

                # 5. Commit actual cost to shared budget
                await self._budget.commit(
                    member=speaker,
                    turns=1,
                    tokens=0,  # per-turn token count not surfaced by FloorMember; cost is the load-bearing axis
                    cost_usd=turn.cost_usd,
                )

                yield FloorTurnEvent(turn=turn, floor_snapshot=self._floor.snapshot())

                # 6. Re-check budget after commit — may have latched exhausted
                if self._budget.is_exhausted():
                    reason = TerminationReason(
                        reason="budget",
                        detail=f"Shared budget latched exhausted after {speaker}'s turn",
                        by_hard_cap=True,
                    )
                    break

            if reason is None:
                reason = TerminationReason(
                    reason="unknown",
                    detail="Floor exited loop without a termination reason",
                )

            yield EnsembleCompletedEvent(
                reason=reason,
                floor_snapshot=self._floor.snapshot(),
                final_transcript=list(self._floor.transcript),
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 — surface as a failed event, don't crash the stream
            logger.exception("[ensemble] pipeline crashed: %s", exc)
            yield EnsembleCompletedEvent(
                reason=TerminationReason(
                    reason="error",
                    detail=f"{type(exc).__name__}: {exc}",
                    by_hard_cap=False,
                ),
                floor_snapshot=self._floor.snapshot(),
                final_transcript=list(self._floor.transcript),
            )


async def ensemble_stream(
    spec: EnsembleSpec,
) -> AsyncGenerator[AgentEvent | FloorTurnEvent | EnsembleCompletedEvent, None]:
    """Drive an ensemble and stream its events. The top-level entry, parallel
    to :func:`~prodagent.runtime.runner.drive_stream` for single agents."""
    pipeline = EnsemblePipeline(spec)
    async for event in pipeline.run():
        yield event
