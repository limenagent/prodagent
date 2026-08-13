"""Blackboard — shared mutable state + declarative triggers."""

from __future__ import annotations

import asyncio
import fnmatch
import logging
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Literal, Protocol, runtime_checkable

from prodagent.backends.memory.lock import InProcessLockStore
from prodagent.core.exceptions import BudgetExceeded
from prodagent.runtime.coordination._stage import StageDriver
from prodagent.runtime.coordination._store import RoundedLockableStore
from prodagent.runtime.coordination.termination import (
    MaxRounds,
    TerminationPolicy,
    TerminationReason,
)

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator, Callable

    from prodagent.ports.lock import LockStore, LockToken
    from prodagent.runtime.agent import Agent
    from prodagent.runtime.coordination.budget_ledger import BudgetLedger

logger = logging.getLogger(__name__)

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
    lock_store: LockStore = field(default_factory=InProcessLockStore)
    """Backs buzz_in arbitration. Defaults to the in-process store — this
    primitive is single-process only, but the port stays swappable."""

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

    async def _compute(self, expert_name: str, trigger: Trigger) -> BoardWrite | None:
        """Reserve → try_contribute → commit for one expert. Shared by both
        modes — the only difference is what gates a candidate reaching here."""
        if self._budget is not None:
            try:
                await self._budget.reserve(member=expert_name, turns=1)
            except BudgetExceeded:
                return None
        member = self._spec.experts[expert_name]
        write = await member.try_contribute(self.board, trigger=trigger)
        if self._budget is not None:
            await self._budget.commit(
                member=expert_name,
                turns=1,
                tokens=write.tokens if write is not None else 0,
                cost_usd=write.cost_usd if write is not None else 0.0,
                reserved_turns=1,
            )
        return write

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
                # Non-blocking try-acquire — a losing candidate must never begin
                # computing. timeout=0 on InProcessLockStore is a true trylock
                # (see backends/memory/lock.py).
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
                    await self.board.write(
                        write.key, write.value, expected_version=write.expected_version
                    )
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
        lines.append(f"  {key} (v{entry['version']}): {entry['value']}")
    return "\n".join(lines)


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

    @property
    def name(self) -> str:
        return self._agent.name

    async def try_contribute(self, board: Board, *, trigger: Trigger) -> BoardWrite | None:
        self._slot.snapshot = board.snapshot()
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
        from prodagent.hooks.checkpoint import InjectionPoint

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
