"""ConversationSession — the cross-turn conversation root."""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from prodagent.base.codec import dump, load
from prodagent.base.types import ExecutionMode, Message, MessageList, RunState

if TYPE_CHECKING:
    from prodagent.base.types import JsonDict
    from prodagent.kernel.state import AgentRun


@dataclass
class TurnRecord:
    """One executed Run's footprint in a Session — append-only, immutable once settled."""

    run_id: str
    mode: ExecutionMode
    state: RunState
    final_output: str | None = None
    started_at: float = field(default_factory=time.time)
    ended_at: float | None = None

    def to_dict(self) -> JsonDict:
        return dump(self)

    @classmethod
    def from_dict(cls, d: JsonDict) -> TurnRecord:
        return load(
            cls,
            d,
            defaults={"mode": ExecutionMode.PLAN_FIRST.value, "state": RunState.RUNNING.value},
        )


@dataclass
class TurnAllocation:
    """Result of ``ConversationSession.start_turn`` for the upcoming Run."""

    run_id: str
    mode: ExecutionMode
    messages: MessageList
    is_new: bool


@dataclass
class ConversationSession:
    """Cross-turn source of truth: owns ``messages`` and run_id allocation."""

    session_id: str
    agent_id: str
    messages: MessageList = field(default_factory=list)
    turns: list[TurnRecord] = field(default_factory=list)
    turn_seq: int = 0
    version: int = 0

    @property
    def last_turn(self) -> TurnRecord | None:
        return self.turns[-1] if self.turns else None

    def start_turn(self, message: str, *, mode: ExecutionMode) -> TurnAllocation:
        """Return a ``TurnAllocation`` for the upcoming Run."""
        # A SUSPENDED run resumes under its original run_id (not a fresh one)
        # so checkpoints, ledger reservations and approval requests stay
        # correlated with the run that awaits them.
        last = self.last_turn
        resume = last is not None and last.state is RunState.SUSPENDED
        self.messages.append(Message(role="user", content=message))
        if resume:
            assert last is not None  # resume implies last_turn exists
            if mode != last.mode:
                raise ValueError(
                    f"session {self.session_id} has a SUSPENDED run under "
                    f"{last.mode}; cannot resume with mode={mode}"
                )
            run_id = last.run_id
            is_new = False
        else:
            self.turn_seq += 1
            run_id = f"{self.session_id}:{self.turn_seq}"
            is_new = True
            self.turns.append(TurnRecord(run_id=run_id, mode=mode, state=RunState.RUNNING))
        return TurnAllocation(run_id, mode, list(self.messages), is_new)

    def complete_turn(self, run_id: str, mode: ExecutionMode, run: AgentRun) -> None:
        """Fold the Run's finished transcript back into the Session."""
        self.messages = list(run.messages)
        record = TurnRecord(
            run_id=run_id,
            mode=mode,
            state=run.state,
            final_output=run.final_output,
            ended_at=time.time(),
        )
        if self.last_turn is not None and self.last_turn.run_id == run_id:
            self.turns[-1] = record  # SUSPENDED turn resumed to completion/suspend
        else:
            self.turns.append(record)

    def to_dict(self) -> JsonDict:
        return dump(self, _raw=frozenset({"messages"}))

    @classmethod
    def from_dict(cls, d: JsonDict) -> ConversationSession:
        return load(cls, d, _raw=frozenset({"messages"}))


__all__ = ["ConversationSession", "TurnAllocation", "TurnRecord"]
