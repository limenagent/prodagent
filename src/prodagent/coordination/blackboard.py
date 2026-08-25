"""Blackboard — shared mutable state + declarative triggers."""

from __future__ import annotations

import fnmatch
import logging
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Literal, Protocol, runtime_checkable

from prodagent.coordination._stage import StageDriver, ViewInjector
from prodagent.coordination._store import RoundedLockableStore
from prodagent.coordination.activation import Activation, ActivationContext, ActivationPolicy
from prodagent.coordination.messaging.envelope import (
    Crossing,
    CrossingKind,
    Direction,
)
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
from prodagent.core.text import bound_text


class BoardVersionConflict(Exception):
    """A board-slot write raced a newer version — the loser is isolated
    (dead-lettered), the board survives. Distinct from core's VersionConflict
    (checkpoint/session optimistic concurrency) on purpose: an AgentError
    subclass would trip the stage driver's terminal-error guard."""


if TYPE_CHECKING:
    from collections.abc import AsyncGenerator, Awaitable, Callable

    from prodagent.coordination.messaging.contract import MessageContract
    from prodagent.coordination.messaging.pipeline import Interceptor
    from prodagent.kernel.budget import BudgetLedger
    from prodagent.kernel.bus import HookRegistry
    from prodagent.ports.dead_letter import DeadLetterStore
    from prodagent.ports.lock import LockStore
    from prodagent.runtime.agent import Agent

logger = logging.getLogger(__name__)

_VALUE_MAX_CHARS_DEFAULT = 2000
"""Admission bound for free-text board values — mirrors
``OrchestrationConfig.handoff_output_max_chars``; override per board via
``BlackboardSpec.value_max_chars``."""

__all__ = [
    "Board",
    "BoardSlot",
    "BoardVersionConflict",
    "Trigger",
    "BlackboardPolicy",
    "BlackboardMember",
    "BoardWrite",
    "BlackboardSpec",
    "BoardWriteEvent",
    "BlackboardCompletedEvent",
    "AgentBlackboardMember",
    "Blackboard",
    "blackboard_stream",
]


# ---------------------------------------------------------------------------
# Board — shared mutable state, optimistic-concurrency writes
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class BoardSlot:
    value: Any
    version: int


class Board(RoundedLockableStore):
    """Shared ``dict[str, BoardSlot]`` — a versioned map of structured fields.

    Not :class:`~prodagent.coordination.floor.SharedFloor`'s append-only
    transcript: Blackboard experts overwrite structured fields, so writes need
    optimistic-concurrency version checks, not just ordering. A
    :class:`RoundedLockableStore` — the lock and round counter come from the base."""

    def __init__(self) -> None:
        super().__init__()
        self._slots: dict[str, BoardSlot] = {}
        self._changes: list[str] = []

    async def write(self, key: str, value: Any, *, expected_version: int | None = None) -> int:
        """Write ``key``, returning the new version. Raises :class:`VersionConflict`
        if ``expected_version`` is given and doesn't match the slot's current
        version (0 for a key that doesn't exist yet)."""
        async with self._lock:
            current = self._slots.get(key)
            current_version = current.version if current is not None else 0
            if expected_version is not None and expected_version != current_version:
                raise BoardVersionConflict(
                    f"write to {key!r} expected version {expected_version}, "
                    f"board has {current_version}"
                )
            new_version = current_version + 1
            self._slots[key] = BoardSlot(value=value, version=new_version)
            self._changes.append(key)
            return new_version

    def read(self, keys: list[str] | None = None) -> dict[str, Any]:
        if keys is None:
            return {k: s.value for k, s in self._slots.items()}
        return {k: self._slots[k].value for k in keys if k in self._slots}

    def version_of(self, key: str) -> int:
        slot = self._slots.get(key)
        return slot.version if slot is not None else 0

    def snapshot(self) -> dict[str, Any]:
        return {
            "slots": {k: {"value": s.value, "version": s.version} for k, s in self._slots.items()},
            "round_count": self._round_count,
            "elapsed_s": time.monotonic() - self.started_at,
        }

    def _drain_changes(self) -> list[str]:
        """Keys written since the last drain — consumed once per pipeline round."""
        changes, self._changes = self._changes, []
        return changes

    def fingerprint(self) -> tuple[int, int]:
        """Liveness fingerprint — the sum of slot versions rises on every write
        and never falls, so it changes iff a write landed this round; the
        un-drained change count disambiguates back-to-back same-version rounds."""
        return (sum(s.version for s in self._slots.values()), len(self._changes))


# ---------------------------------------------------------------------------
# Trigger + BlackboardMember
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Trigger:
    """Declarative rule: when board keys matching ``keys`` change, run ``experts``.

    ``keys=[]`` means "always matches" (fires every round) — useful for a
    kickoff expert seeding the board, or a poller that returns ``None`` (via
    ``try_contribute``) once it has nothing left to add.
    """

    name: str
    keys: list[str]
    experts: list[str]
    mode: Literal["event", "buzz_in"] = "event"

    def matches(self, changed_keys: list[str]) -> bool:
        if not self.keys:
            return True
        return any(fnmatch.fnmatch(k, pattern) for k in changed_keys for pattern in self.keys)


class BlackboardPolicy:
    """Adapts the Trigger list to :class:`~prodagent.coordination.activation.ActivationPolicy`.

    Each matched trigger becomes one :class:`Activation` this round: ``event``
    mode fans out concurrently, ``buzz_in`` races for a single winner — the
    same two dispatch shapes :meth:`StageDriver._dispatch` already gives
    Ensemble and WorkQueue, so Blackboard no longer hand-rolls its own."""

    def __init__(self, triggers: dict[str, Trigger]) -> None:
        self._triggers = triggers

    async def next_activations(self, ctx: ActivationContext) -> list[Activation]:
        changed = list(ctx.changed_keys)
        matched = [t for t in self._triggers.values() if t.matches(changed)]
        return [
            Activation(
                members=list(t.experts),
                dispatch="single_winner" if t.mode == "buzz_in" else "concurrent",
                round_num=ctx.round_num,
                label=t.name,
            )
            for t in matched
        ]


@dataclass
class BoardWrite:
    """One expert's contribution, folded into the :class:`Board` by the pipeline."""

    key: str
    value: Any
    author: str
    expected_version: int | None = None
    cost_usd: float = 0.0
    tokens: int = 0


@runtime_checkable
class BlackboardMember(Protocol):
    """What it takes to be a Blackboard expert. Unlike
    :class:`~prodagent.coordination.floor.FloorMember.speak` (must
    return a turn), ``try_contribute`` may return ``None`` — "this trigger
    fired but I have nothing to add, don't occupy a write slot for it"."""

    name: str

    async def try_contribute(self, board: Board, *, trigger: Trigger) -> BoardWrite | None: ...


# ---------------------------------------------------------------------------
# BlackboardSpec
# ---------------------------------------------------------------------------


def _in_process_lock() -> LockStore:
    from prodagent.backends.factory import in_process_lock_store

    return in_process_lock_store()


@dataclass
class BlackboardSpec:
    experts: dict[str, BlackboardMember]
    triggers: dict[str, Trigger]
    termination: TerminationPolicy = field(
        default_factory=lambda: TerminationPolicy(hard_cap=MaxRounds(max_rounds=20))
    )
    budget: BudgetLedger | None = None
    terminal_check: Callable[[Board], bool] | None = None
    """Business-level "the board is done" check — e.g. all required keys filled.
    Checked before each round, independent of TerminationPolicy."""
    lock_store: LockStore = field(default_factory=lambda: _in_process_lock())

    """Backs buzz_in arbitration. Defaults to the in-process store — this
    primitive is single-process only, but the port stays swappable."""

    contracts: dict[str, MessageContract] | None = None
    """Per-key admission contracts for written values. A key with no declared
    contract is admitted as-is — the board is an open workspace by default,
    apps declare shapes for the keys that matter."""

    hooks: HookRegistry | None = None
    """Registry for the write-admission gate. ``None`` (default) keeps the
    gate dormant — pass a registry once you register AGENT_HANDOFF checkers."""

    dead_letter: DeadLetterStore | None = None
    """Where rejected writes (and lost version races) land. ``None`` (default)
    resolves the framework's dead-letter backend."""

    value_max_chars: int = 0
    """Admission bound for free-text values. ``0`` → framework default."""

    write_interceptors: list[tuple[Slot, Interceptor]] = field(default_factory=list)
    """User-injected semantics on the write pipeline — mounted at their
    declared slots, order preserved."""

    def __post_init__(self) -> None:
        if not self.experts:
            raise ValueError("BlackboardSpec.experts cannot be empty")
        if not self.triggers:
            raise ValueError("BlackboardSpec.triggers cannot be empty")
        for trigger in self.triggers.values():
            unknown = [e for e in trigger.experts if e not in self.experts]
            if unknown:
                raise ValueError(
                    f"trigger {trigger.name!r} references unknown expert(s) {unknown} — "
                    f"known experts: {list(self.experts)}"
                )


# ---------------------------------------------------------------------------
# Events
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class BoardWriteEvent:
    write: BoardWrite
    trigger_name: str
    board_snapshot: dict[str, Any]


@dataclass(frozen=True, slots=True)
class BlackboardCompletedEvent:
    reason: TerminationReason
    board_snapshot: dict[str, Any]


# ---------------------------------------------------------------------------
# Pipeline
# ---------------------------------------------------------------------------


class Blackboard(StageDriver[BoardWriteEvent | BlackboardCompletedEvent]):
    """Drives a Blackboard: each round, scan triggers against keys that changed
    last round, dispatch matching triggers (event = concurrent fan-out,
    buzz_in = lock-first-then-compute), fold writes back in.

    Crash→error and finalize-to-unknown are handled by :class:`StageDriver`."""

    def __init__(self, spec: BlackboardSpec) -> None:
        from prodagent.backends.factory import resolve_dead_letter

        super().__init__()
        self._spec = spec
        self.board = Board()
        self._budget = spec.budget
        self._value_max_chars = spec.value_max_chars or _VALUE_MAX_CHARS_DEFAULT
        self._activation: ActivationPolicy = BlackboardPolicy(spec.triggers)
        self._trigger_by_name: dict[str, Trigger] = {t.name: t for t in spec.triggers.values()}
        self._dlq: DeadLetterStore = (
            spec.dead_letter if spec.dead_letter is not None else resolve_dead_letter(None)
        )
        # Write admission (UPSTREAM): an expert's value enters a board slot —
        # the shared state every downstream expert reads — through the
        # messaging plane. The crossing's payload is the *value* itself; the
        # envelope carries key (to), author (from_agent), and version lineage.
        self._write_pipeline: Pipeline = admission_pipeline(
            contract=self._contract_for,
            trim=self._bound_value,
            hooks=spec.hooks,
            dead_letter=self._dlq,
        )
        for slot, interceptor in spec.write_interceptors:
            self._write_pipeline.add(slot, interceptor)

    def _contract_for(self, crossing: Crossing[Any]) -> MessageContract | None:
        """Per-key contracts declared on the spec; undeclared keys admit as-is."""
        if self._spec.contracts:
            return self._spec.contracts.get(crossing.to)
        return None

    def _bound_value(self, value: Any) -> Any:
        """Cap free-text values — one verbose expert must not blow every other
        expert's context window. Structured values are the contracts' business."""
        if isinstance(value, str):
            return bound_text(value, self._value_max_chars)
        return value

    async def _compute(self, expert_name: str, trigger: Trigger) -> BoardWrite | None:
        """Reserve → try_contribute → commit for one expert, via the shared
        :meth:`StageDriver._run_enveloped`. Shared by both modes — the only
        difference is what gates a candidate reaching here."""
        member = self._spec.experts[expert_name]
        produced: list[BoardWrite | None] = []

        async def _act() -> tuple[int, float] | None:
            write = await member.try_contribute(self.board, trigger=trigger)
            produced.append(write)
            if write is None:
                return None
            return write.tokens, write.cost_usd

        await self._run_enveloped(expert_name, _act)
        return produced[0] if produced else None

    def _member_runner(self, trigger: Trigger) -> Callable[[str], Awaitable[BoardWrite | None]]:
        """Bind ``trigger`` into a ``(name) -> BoardWrite | None`` callable —
        the shape :meth:`StageDriver._dispatch` runs per activation member."""

        async def run_one(name: str) -> BoardWrite | None:
            return await self._compute(name, trigger)

        return run_one

    async def _rounds(self) -> AsyncGenerator[BoardWriteEvent, None]:
        """One round per iteration: ask :class:`BlackboardPolicy` which triggers
        matched → dispatch each activation via :meth:`StageDriver._dispatch`
        (concurrent fan-out or single-winner lock-race) → fold writes. Sets
        ``self._reason`` when the board should stop. Crash→error and
        finalize-to-unknown are handled by :meth:`StageDriver.run`."""
        round_num = 0
        while True:
            stop, policy_reason = self._spec.termination.should_stop(
                self.board, next_round=round_num
            )
            if stop and policy_reason is not None:
                self._reason = policy_reason
                break

            if self._spec.terminal_check is not None and self._spec.terminal_check(self.board):
                self._reason = TerminationReason(
                    reason="terminal_check", detail="Board satisfied terminal_check"
                )
                break

            self.board._advance_round(round_num)
            changed = self.board._drain_changes() if round_num > 0 else []
            activations = await self._activation.next_activations(
                ActivationContext(
                    store=self.board, changed_keys=tuple(changed), round_num=round_num
                )
            )
            if not activations:
                self._reason = TerminationReason(
                    reason="quiescent",
                    detail="No trigger matched — board has no pending work",
                )
                break

            before = self.board.fingerprint()
            for activation in activations:
                trigger = self._trigger_by_name[activation.label]
                results = await self._dispatch(
                    activation,
                    self._member_runner(trigger),
                    lock_store=self._spec.lock_store,
                    lock_scope=f"board:{id(self.board)}",
                )
                for _name, write in results:
                    if write is None:
                        continue
                    delivery = await self._write_pipeline.process(
                        Crossing.mint(
                            direction=Direction.UPSTREAM,
                            kind=CrossingKind.WRITE,
                            from_agent=write.author,
                            to=write.key,
                            payload=write.value,
                            trigger=trigger.name,
                            round=round_num,
                        )
                    )
                    if delivery.status != "delivered":
                        # Rejected by contract or gate — this expert's
                        # contribution is faulted, the board carries on.
                        logger.warning(
                            "[blackboard] write by %s to %r not admitted (%s): %s",
                            write.author,
                            write.key,
                            delivery.status,
                            delivery.reason[:120],
                        )
                        continue
                    write.value = delivery.crossing.payload
                    try:
                        await self.board.write(
                            write.key, write.value, expected_version=write.expected_version
                        )
                    except BoardVersionConflict as exc:
                        # Lost an optimistic-concurrency race — dead letter the
                        # losing write and keep the board alive. (Previously
                        # this escaped to the StageDriver crash guard and
                        # killed the whole run.)
                        logger.warning(
                            "[blackboard] version conflict on %r by %s: %s — write dropped",
                            write.key,
                            write.author,
                            exc,
                        )
                        await self._dlq.on_failure(
                            f"{trigger.name}:{write.author}:{write.key}",
                            {"kind": "write", "from_agent": write.author, "to": write.key},
                            str(exc),
                        )
                        continue
                    yield BoardWriteEvent(
                        write=write,
                        trigger_name=trigger.name,
                        board_snapshot=self.board.snapshot(),
                    )

            if self.board.fingerprint() == before:
                self._reason = TerminationReason(
                    reason="no_contribution",
                    detail=f"Matched trigger(s) {[a.label for a in activations]} produced no writes",
                )
                break

            round_num += 1

    def _completed(self, reason: TerminationReason) -> BoardWriteEvent | BlackboardCompletedEvent:
        return BlackboardCompletedEvent(reason=reason, board_snapshot=self.board.snapshot())


async def blackboard_stream(
    spec: BlackboardSpec,
) -> AsyncGenerator[BoardWriteEvent | BlackboardCompletedEvent, None]:
    """Drive a Blackboard and stream its events — parallel to ``ensemble_stream``."""
    pipeline = Blackboard(spec)
    async for event in pipeline.run():
        yield event


# ---------------------------------------------------------------------------
# AgentBlackboardMember — adapt a prodagent Agent to BlackboardMember
# ---------------------------------------------------------------------------


class _BoardViewSlot:
    """Mutable slot the ``[BOARD]`` injector reads — same pattern as
    ensemble's ``_FloorViewSlot``: pipeline writes the view before each
    ``try_contribute()``, injector reads it during context assembly."""

    __slots__ = ("snapshot", "trigger_name")

    def __init__(self) -> None:
        self.snapshot: dict[str, Any] = {}
        self.trigger_name: str = ""


def _format_board_block(slot: _BoardViewSlot) -> str:
    if not slot.snapshot:
        return ""
    lines = [f"[BOARD] trigger: {slot.trigger_name}", "state:"]
    for key, entry in slot.snapshot.get("slots", {}).items():
        rendered = _render_value(entry["value"], _VALUE_MAX_CHARS_DEFAULT)
        lines.append(f"  {key} (v{entry['version']}): {rendered}")
    return "\n".join(lines)


def _render_value(value: Any, max_chars: int) -> str:
    """Bounded rendering of a slot value — every expert's context gets the
    same guarantee the floor's projection gives its members."""
    text = value if isinstance(value, str) else repr(value)
    if len(text) > max_chars:
        return bound_text(text, max_chars)
    return text


class AgentBlackboardMember:
    """Adapts a full :class:`~prodagent.runtime.agent.Agent` to :class:`BlackboardMember`.

    Registers a ``[BOARD]`` injector (mirrors ensemble's ``[FLOOR]``) so the
    board's current state lands in L2 alongside ``[MEMORY]``. The agent is
    prompted to propose a ``key: value`` write or reply ``PASS`` — this is a
    reference implementation; callers with structured-output needs should
    implement :class:`BlackboardMember` directly against their own agent
    instead of parsing free text."""

    def __init__(self, agent: Agent, *, session_id: str, write_key: str) -> None:
        self._agent = agent
        self._session_id = session_id
        self._write_key = write_key
        self._slot = _BoardViewSlot()
        self._view_injector = ViewInjector(
            agent, block="BOARD", render=lambda: _format_board_block(self._slot)
        )
        self._view_pipe: Pipeline | None = None

    @property
    def name(self) -> str:
        return self._agent.name

    def _view_pipeline(self) -> Pipeline:
        """DOWNSTREAM view pipeline: the board snapshot enters this expert's
        context through the plane (gate fires only if checkers registered)."""
        if self._view_pipe is None:
            self._view_pipe = assembly_pipeline(hooks=self._agent.hooks)
        return self._view_pipe

    async def try_contribute(self, board: Board, *, trigger: Trigger) -> BoardWrite | None:
        delivery = await self._view_pipeline().process(
            Crossing.mint(
                direction=Direction.DOWNSTREAM,
                kind=CrossingKind.DISPATCH,
                from_agent="board",
                to=self.name,
                payload=board.snapshot(),
            )
        )
        self._slot.snapshot = delivery.crossing.payload
        self._slot.trigger_name = trigger.name
        self._view_injector.wire_once()

        prompt = (
            f"Trigger {trigger.name!r} fired. Review the [BOARD] state above. "
            f"If you have a contribution for key {self._write_key!r}, reply with just "
            "the value. If you have nothing to add this round, reply exactly PASS."
        )
        try:
            run = await self._agent.chat(prompt, session_id=self._session_id)
        except Exception as exc:  # noqa: BLE001 — a member failing shouldn't kill the board
            logger.warning(
                "[blackboard] member %s try_contribute() raised %s: %s — treating as pass",
                self.name,
                type(exc).__name__,
                exc,
            )
            return None

        output = (run.final_output or "").strip()
        if not output or output.upper() == "PASS":
            return None
        return BoardWrite(
            key=self._write_key,
            value=output,
            author=self.name,
            cost_usd=float(getattr(run, "cost_usd", 0.0) or 0.0),
            tokens=int(getattr(run, "input_tokens", 0) or 0)
            + int(getattr(run, "output_tokens", 0) or 0),
        )
