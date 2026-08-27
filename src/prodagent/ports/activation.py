"""ActivationPolicy — the cross-primitive concept of "who acts next".

Lifted from ``coordination/activation.py`` to ports: activation is wire
vocabulary. Every scheduler in the framework — the three stage primitives
today, graph nodes and remote dispatchers tomorrow — asks the same question:
given the current shared state and what changed last round, which members
wake up this round, and do they run serially, concurrently, or does only one
of them win? This module names that concept once:

- :class:`Activation` — one unit of scheduled work: members + dispatch mode +
  why (a label for logs/events).
- :class:`ActivationPolicy` — the protocol each primitive's config adapts to.
  ``Ensemble``'s :class:`~prodagent.coordination.ensemble.SpeakingOrder`,
  ``Blackboard``'s :class:`~prodagent.coordination.blackboard.Trigger`
  list, and ``WorkQueue``'s worker set each become a thin adapter, so a new
  coordination style (an LLM moderator, a priority queue, a pub/sub topic) is
  a new adapter — not a new round loop.

The stores policies read stay in coordination (their write semantics are real
domain content); :class:`StageStore` below is the narrow read-side contract
they're seen through, satisfied structurally by
:class:`~prodagent.coordination._store.SharedStore`.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Literal, Protocol, runtime_checkable

if TYPE_CHECKING:
    from collections.abc import Awaitable

__all__ = [
    "DispatchMode",
    "StageStore",
    "Activation",
    "ActivationPolicy",
    "ActivationContext",
]


DispatchMode = Literal["serial", "concurrent", "single_winner"]
"""How an activation's members run:

- ``serial`` — one at a time, in member order (round-robin floor, moderated pick).
- ``concurrent`` — all at once, results collected together (event-mode trigger
  fan-out, a work-queue round's claim race).
- ``single_winner`` — all *race*, but only one computes (buzz_in: the first to
  grab the lock wins; losers must never start real work).
"""


@runtime_checkable
class StageStore(Protocol):
    """Read-side contract of a stage's shared store, as an activation policy
    sees it. Satisfied structurally by coordination's ``SharedStore`` family;
    policies that need a concrete store's specifics down-cast in their own
    layer."""

    def round_count(self) -> int: ...

    def snapshot(self) -> dict[str, Any]: ...

    def fingerprint(self) -> Any: ...


@dataclass(frozen=True)
class Activation:
    """One scheduled batch of member activations.

    ``round_num`` is the round this batch belongs to — computed by the policy,
    because "when does a round advance" is order-specific (round-robin wraps by
    member position; a free-for-all advances every batch; a trigger board
    advances every drain cycle).
    """

    members: list[str]
    dispatch: DispatchMode = "serial"
    round_num: int = 0
    label: str = ""
    """Why this activation exists — trigger name, order name, "pull". For logs/events."""

    def why(self) -> str:
        return self.label or ",".join(self.members)


@dataclass(frozen=True)
class ActivationContext:
    """What an ActivationPolicy sees when deciding the next activation(s).

    ``store`` is the live shared store (floor / board / queue — read-only from
    the policy's perspective). ``changed_keys`` is what mutated last round:
    the board's drained change list; empty/None for stores whose writes aren't
    key-shaped (transcripts, queue transitions).
    """

    store: StageStore
    changed_keys: tuple[str, ...] = ()
    round_num: int = 0
    """The round the *next* activation would run in (same convention as
    ``TerminationStrategy.should_stop(next_round=...)``)."""


@runtime_checkable
class ActivationPolicy(Protocol):
    """Decides who acts next. Returns one or more :class:`Activation` batches
    for the coming round, or an empty list when there is no pending work —
    which the driver surfaces as its quiescent/no-activation stop reason.

    Async by design: a moderated picker may await an LLM to name the next
    speaker."""

    def next_activations(self, ctx: ActivationContext) -> Awaitable[list[Activation]]: ...
