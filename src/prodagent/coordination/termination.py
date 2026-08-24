"""TerminationPolicy — when does a floor/queue/board stop?

Composite by design: an optional *business* strategy (moderator verdict,
consensus vote, convergence) decides graceful end; a *mandatory* hard cap
(:class:`MaxRounds`) guarantees stop even if business never fires. Mirrors
:class:`~prodagent.kernel.budget.HardBudget` — business is "end elegantly",
cap is "stop, no matter what". Business may be ``None``; the cap may not —
an unattended ensemble that never stops is a cost runaway.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Protocol, runtime_checkable

logger = logging.getLogger(__name__)

__all__ = [
    "RoundCountable",
    "TerminationStrategy",
    "MaxRounds",
    "TerminationPolicy",
    "TerminationReason",
    "evaluate_termination",
]


@runtime_checkable
class RoundCountable(Protocol):
    """Anything with a ``round_count()`` — :class:`SharedFloor`, :class:`Board`,
    :class:`SharedQueue` all satisfy this. TerminationPolicy only needs the
    round count, so it depends on this narrow protocol, not the concrete
    floor/queue/board classes."""

    def round_count(self) -> int: ...


@dataclass
class TerminationReason:
    """Why the floor/queue/board stopped — carried into the final event."""

    reason: str
    """Short code: ``max_rounds`` / ``moderator`` / ``consensus`` / ``convergence`` / ``budget``."""

    detail: str = ""
    """Human-readable elaboration — which axis, which round, etc."""

    by_hard_cap: bool = False
    """True if the hard cap fired (vs. a graceful business-strategy stop)."""


@runtime_checkable
class TerminationStrategy(Protocol):
    """Business termination — may never fire; the hard cap backs it."""

    def should_stop(
        self, floor: RoundCountable, *, next_round: int
    ) -> tuple[bool, TerminationReason | None]:
        """Return ``(stop, reason)``. ``next_round`` is the round the next
        speaker would speak *in* — lets a strategy veto "don't start round N"
        before anyone speaks. ``reason`` None = no verdict, distinct from
        "verdict: stop"."""
        ...


@dataclass
class MaxRounds:
    """Hard cap on rounds. Always present, never None.

    ``max_rounds=N`` means "no member speaks in round N or later" — so
    ``max_rounds=2`` allows rounds 0 and 1, i.e. ``2 × N`` turns for an
    N-member round-robin. Checked *before* the next speaker is scheduled —
    same semantics as :class:`~prodagent.kernel.budget.HardBudget`'s turn axis:
    check before the next unit of work, not mid-work.
    """

    max_rounds: int = 10

    def should_stop(
        self, floor: RoundCountable, *, next_round: int
    ) -> tuple[bool, TerminationReason | None]:
        if next_round >= self.max_rounds:
            return True, TerminationReason(
                reason="max_rounds",
                detail=(
                    f"Floor would enter round {next_round} (cap {self.max_rounds}) — "
                    f"completed {floor.round_count()} round(s)"
                ),
                by_hard_cap=True,
            )
        return False, None


@dataclass
class TerminationPolicy:
    """Composite: optional business strategy AND mandatory hard cap.

    Pipeline evaluates ``business.should_stop() OR hard_cap.should_stop()``
    each round; first to fire wins. If both would fire, business's reason is
    preferred (graceful stop is more informative than "hit the cap").
    """

    hard_cap: MaxRounds
    business: TerminationStrategy | None = None

    def __post_init__(self) -> None:
        if self.hard_cap is None:
            raise ValueError(
                "TerminationPolicy.hard_cap cannot be None — the hard cap is the "
                "backstop that guarantees an unattended ensemble stops. Pass "
                "MaxRounds(max_rounds=...) explicitly."
            )
        if self.hard_cap.max_rounds < 1:
            raise ValueError(f"MaxRounds.max_rounds must be >= 1, got {self.hard_cap.max_rounds}")

    def should_stop(
        self, floor: RoundCountable, *, next_round: int
    ) -> tuple[bool, TerminationReason | None]:
        if self.business is not None:
            stop, reason = self.business.should_stop(floor, next_round=next_round)
            if stop:
                return True, reason
        return self.hard_cap.should_stop(floor, next_round=next_round)


def evaluate_termination(
    policy: TerminationPolicy,
    floor: RoundCountable,
    *,
    next_round: int,
) -> TerminationReason:
    """Evaluate policy. Budget is *not* checked here — the pipeline owns the
    :class:`SharedBudget` and checks it separately (it's the hardest stop,
    independent of policy)."""
    stop, reason = policy.should_stop(floor, next_round=next_round)
    if stop and reason is not None:
        return reason
    return TerminationReason(reason="continue", detail="no termination condition met")
