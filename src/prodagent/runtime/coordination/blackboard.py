"""Blackboard — shared mutable state + declarative triggers."""

from __future__ import annotations

import asyncio
import fnmatch
import logging
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Literal, Protocol, runtime_checkable

from prodagent.bootstrap import in_process_lock_store, resolve_dead_letter
from prodagent.runtime.coordination._stage import StageDriver
from prodagent.runtime.coordination._store import RoundedLockableStore
from prodagent.runtime.coordination.messaging.envelope import (
    Crossing,
    CrossingKind,
    Direction,
)
from prodagent.runtime.coordination.messaging.pipeline import (
    Pipeline,
    Slot,
    admission_pipeline,
    assembly_pipeline,
)
from prodagent.runtime.coordination.termination import (
    MaxRounds,
    TerminationPolicy,
    TerminationReason,
)

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator, Callable

    from prodagent.hooks.registry import HookRegistry
    from prodagent.ports.dead_letter import DeadLetterStore
    from prodagent.ports.lock import LockStore, LockToken
    from prodagent.runtime.agent import Agent
    from prodagent.runtime.coordination.budget_ledger import BudgetLedger
    from prodagent.runtime.coordination.messaging.contract import MessageContract
    from prodagent.runtime.coordination.messaging.pipeline import Interceptor

logger = logging.getLogger(__name__)

_VALUE_MAX_CHARS_DEFAULT = 2000
"""Admission bound for free-text board values — mirrors
``OrchestrationConfig.handoff_output_max_chars``; override per board via
``BlackboardSpec.value_max_chars``."""

__all__ = [
    "Board",
    "BoardSlot",
    "VersionConflict",
    "Trigger",
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


class VersionConflict(Exception):
    """Raised by :meth:`Board.write` when ``expected_version`` is stale."""


@dataclass(frozen=True, slots=True)
class BoardSlot:
    value: Any
    version: int


class Board(RoundedLockableStore):
    """Shared ``dict[str, BoardSlot]`` — a versioned map of structured fields.

    Not :class:`~prodagent.runtime.coordination.floor.SharedFloor`'s append-only
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
                raise VersionConflict(
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
    :class:`~prodagent.runtime.coordination.floor.FloorMember.speak` (must
    return a turn), ``try_contribute`` may return ``None`` — "this trigger
    fired but I have nothing to add, don't occupy a write slot for it"."""

    name: str

    async def try_contribute(self, board: Board, *, trigger: Trigger) -> BoardWrite | None: ...


# ---------------------------------------------------------------------------
# BlackboardSpec
# ---------------------------------------------------------------------------


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
    lock_store: LockStore = field(default_factory=in_process_lock_store)
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
        super().__init__()
        self._spec = spec
        self.board = Board()
        self._budget = spec.budget
        self._value_max_chars = spec.value_max_chars or _VALUE_MAX_CHARS_DEFAULT
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
        if isinstance(value, str) and len(value) > self._value_max_chars:
            return value[: self._value_max_chars] + (
                f"\n…(truncated, {len(value) - self._value_max_chars} more chars)"
            )
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

    async def _dispatch_event(self, trigger: Trigger) -> list[BoardWrite | None]:
        return list(
            await asyncio.gather(*(self._compute(name, trigger) for name in trigger.experts))
        )

    async def _dispatch_buzz_in(self, trigger: Trigger) -> list[BoardWrite | None]:
        """Lock-first-then-compute: race for one lock, then only the winner computes.

        Race and compute are two separate phases. If each candidate
        acquired-and-released independently, a candidate whose
        ``try_contribute`` never actually suspends (no real ``await`` inside)
        would finish and release before the next candidate's task is even
        scheduled — the "loser" would see a free lock and become a second
        winner in the same round. Holding the lock across the whole race
        (release only after the sole winner computes) closes that: every other
        candidate's one-shot ``acquire(timeout=0)`` sees it already held for
        the entire duration of this trigger's dispatch."""
        lock_name = f"board:{id(self.board)}:{trigger.name}"
        winner: str | None = None
        token: LockToken | None = None

        async def _race(name: str) -> None:
            nonlocal winner, token
            try:
                acquired = await self._spec.lock_store.acquire(lock_name, timeout=0)
            except TimeoutError:
                return
            winner, token = name, acquired

        await asyncio.gather(*(_race(name) for name in trigger.experts))

        if winner is None or token is None:
            return [None] * len(trigger.experts)
        won_token = token
        try:
            write = await self._compute(winner, trigger)
        finally:
            await self._spec.lock_store.release(won_token)
        return [write if name == winner else None for name in trigger.experts]

    async def _rounds(self) -> AsyncGenerator[BoardWriteEvent, None]:
        """One round per iteration: match changed-key triggers → dispatch
        (event fan-out or buzz_in lock-race) → fold writes. Sets
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
            matched = [t for t in self._spec.triggers.values() if t.matches(changed)]
            if not matched:
                self._reason = TerminationReason(
                    reason="quiescent",
                    detail="No trigger matched — board has no pending work",
                )
                break

            before = self.board.fingerprint()
            for trigger in matched:
                dispatch = (
                    self._dispatch_buzz_in if trigger.mode == "buzz_in" else self._dispatch_event
                )
                results = await dispatch(trigger)
                for write in results:
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
                    except VersionConflict as exc:
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
                    detail=f"Matched trigger(s) {[t.name for t in matched]} produced no writes",
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
        return text[:max_chars] + f"\n…(truncated, {len(text) - max_chars} more chars)"
    return text


def _make_board_injector(slot: _BoardViewSlot) -> Any:
    async def _injector(**kw: Any) -> str:
        return _format_board_block(slot)

    return _injector


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
        self._injector_wired = False
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
        self._wire_board_injector_once()

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

    def _wire_board_injector_once(self) -> None:
        if self._injector_wired:
            return
        from prodagent.hooks.gates import InjectionPoint

        hooks = self._agent.hooks
        if hooks is None:
            hooks = self._agent.attach_default_hooks()
        if hooks is None:
            logger.warning(
                "[blackboard] agent %s has no hooks registry — [BOARD] block "
                "will not be injected; member won't see board state",
                self.name,
            )
            return
        hooks.register_injector(InjectionPoint.CONTEXT_INJECTOR, _make_board_injector(self._slot))
        self._injector_wired = True
