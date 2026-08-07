"""TerminationPolicy — when does an ensemble floor stop?

The composite structure is load-bearing: a *business* strategy (moderator
verdict, consensus vote, convergence detection) decides when the conversation
has reached a graceful end; a *hard cap* (``MaxRounds``) guarantees it stops
even if the business strategy never fires. The two are not peers and the hard
cap is not optional — an LLM-judging-moderator can fail to converge, a
consensus vote can sit below quorum forever, and an unattended ensemble that
never stops is a cost runaway.

This mirrors :class:`~prodagent.core.budget.HardBudget`'s philosophy: business
logic is "try to end elegantly", the hard cap is "stop, no matter what". The
cap is welded into :class:`TerminationPolicy` as a non-optional field — the
pipeline never accepts a policy without one.

A business strategy is allowed to be ``None`` (the minimal-closed-loop case:
just run N rounds and stop). The hard cap is not.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol, runtime_checkable

if TYPE_CHECKING:
    from prodagent.runtime.coordination.floor import SharedFloor

logger = logging.getLogger(__name__)

__all__ = [
    "TerminationStrategy",
    "MaxRounds",
    "TerminationPolicy",
    "TerminationReason",
    "evaluate_termination",
]


@dataclass
class TerminationReason:
    """Why the floor stopped — carried into the final event / checkpoint."""

    reason: str
    """Short code: ``max_rounds`` / ``moderator`` / ``consensus`` / ``convergence`` / ``budget``."""

    detail: str = ""
    """Human-readable elaboration — which axis, which round, etc."""

    by_hard_cap: bool = False
    """True if the hard cap fired (vs. a graceful business-strategy stop)."""


@runtime_checkable
class TerminationStrategy(Protocol):
    """Business termination — may never fire, that's fine, the hard cap backs it."""

    def should_stop(
        self, floor: SharedFloor, *, next_round: int
    ) -> tuple[bool, TerminationReason | None]:
        """Return ``(stop, reason)``. ``next_round`` is the round index the
        next speaker would speak *in* — lets a strategy decide "don't start
        round N" before anyone speaks in it. ``reason`` is None if not
        stopping, letting the caller distinguish "no verdict" from "verdict:
        stop"."""
        ...


@dataclass
class MaxRounds:
    """Hard cap on floor rounds. Always present, never None.

    A "round" is one full pass of the speaking order (in round-robin, that's
    one turn per member). ``max_rounds=N`` means "no member speaks in round N
    or later" — so ``max_rounds=2`` allows rounds 0 and 1, i.e. ``2 × N`` turns
    for an N-member round-robin. The check happens *before* the next speaker
    is scheduled, using the planned round index — same semantics as
    :class:`~prodagent.core.budget.HardBudget`'s turn axis: check before the
    next unit of work, not in the middle of one.
    """

    max_rounds: int = 10

    def should_stop(
        self, floor: SharedFloor, *, next_round: int
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

    The hard cap is not a default the caller can override with ``None`` —
    construction itself enforces it. The pipeline evaluates
    ``business.should_stop() OR hard_cap.should_stop()`` each round; whichever
    fires first wins, and if both would fire the business strategy's reason is
    preferred (graceful stop is more informative than "hit the cap").
    """

    hard_cap: MaxRounds
    business: TerminationStrategy | None = None

    def __post_init__(self) -> None:
        # The hard cap is the load-bearing guarantee. A None hard cap would
        # mean "rely on the business strategy to stop" — that's exactly the
        # failure mode this composite exists to prevent. Reject it at
        # construction, not at runtime.
        if self.hard_cap is None:
            raise ValueError(
                "TerminationPolicy.hard_cap cannot be None — the hard cap is the "
                "backstop that guarantees an unattended ensemble stops. Pass "
                "MaxRounds(max_rounds=...) explicitly."
            )
        if self.hard_cap.max_rounds < 1:
            raise ValueError(f"MaxRounds.max_rounds must be >= 1, got {self.hard_cap.max_rounds}")

    def should_stop(
        self, floor: SharedFloor, *, next_round: int
    ) -> tuple[bool, TerminationReason | None]:
        # Business strategy first — if it gracefully reports stop, that's the
        # more informative reason. Falls through to hard cap otherwise.
        if self.business is not None:
            stop, reason = self.business.should_stop(floor, next_round=next_round)
            if stop:
                return True, reason
        return self.hard_cap.should_stop(floor, next_round=next_round)


def evaluate_termination(
    policy: TerminationPolicy,
    floor: SharedFloor,
    *,
    next_round: int,
) -> TerminationReason:
    """Evaluate policy, returning a final reason.

    Budget exhaustion is *not* checked here — the pipeline holds the
    :class:`SharedBudget` and checks it separately (it's the hardest stop,
    independent of policy). This helper is purely the policy-level evaluation.
    """
    stop, reason = policy.should_stop(floor, next_round=next_round)
    if stop and reason is not None:
        return reason
    return TerminationReason(reason="continue", detail="no termination condition met")
