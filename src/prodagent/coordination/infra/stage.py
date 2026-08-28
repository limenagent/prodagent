"""StageDriver — the shared streaming lifecycle for stage coordination primitives.

``Ensemble``, ``Blackboard`` and ``WorkQueue`` are the three *stage* primitives:
top-level drivers that stream events round after round until something stops
them, then emit exactly one terminal *Completed* event. Their round *bodies*
differ on purpose — an ensemble picks a speaker, a blackboard matches triggers, a
work queue sweeps leases and fans out workers — and so do their stop *reasons*
(an ensemble reports ``budget``; a blackboard reports ``no_contribution`` when a
blocked reserve starves a round; a work queue reports ``drained`` / ``no_progress``).
Those stay in each subclass; forcing them identical would erase real semantics.

What *is* identical across the three — and therefore lives here, once — is the
infrastructure around the loop:

- the lifecycle guard (a raise becomes a terminal ``error`` event; a loop that
  ends without a reason finalizes to ``unknown``);
- the dispatch interpreter for an :class:`~prodagent.ports.activation.Activation`
  (serial / concurrent with fail-fast cancel / single-winner lock race);
- the termination policy (business strategy ∧ mandatory hard cap) and its
  ``TerminationReason`` vocabulary;
- the budget envelope around each unit of work (reserve → act → commit via
  ``kernel.budget.run_enveloped``);
- ``has_durable_events`` — the restore-or-fresh decision every durable stage
  makes before its first round.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Generic, Protocol, TypeVar, runtime_checkable

from prodagent.kernel.budget import run_enveloped


class ViewInjector:
    """Wire-once CONTEXT_INJECTOR registration for a stage member's view.

    The stage (floor/board) refreshes a view slot externally before each
    turn; this class only owns register-once semantics on the member's hook
    registry — wiring must happen before the first ``chat()`` resolves one."""

    def __init__(self, agent: Any, *, block: str, render: Callable[[], str]) -> None:
        self._agent = agent
        self._block = block
        self._render = render
        self._wired = False

    def wire_once(self) -> None:
        if self._wired:
            return
        from prodagent.kernel.bus import InjectionPoint

        hooks = self._agent.hooks
        if hooks is None:
            hooks = self._agent.attach_default_hooks()
        if hooks is None:
            logger.warning(
                "[%s] agent %s has no hooks registry — [%s] block will not "
                "be injected; member won't see the shared view",
                self._block,
                getattr(self._agent, "name", "?"),
                self._block,
            )
            return
        hooks.register_injector(InjectionPoint.CONTEXT_INJECTOR, self._inject)
        self._wired = True

    async def _inject(self, **kw: Any) -> str:
        return self._render()


if TYPE_CHECKING:
    from collections.abc import AsyncGenerator, Awaitable, Callable

    from prodagent.kernel.budget import BudgetLedger
    from prodagent.ports.activation import Activation
    from prodagent.ports.lock import LockStore, LockToken

logger = logging.getLogger(__name__)

E = TypeVar("E")

__all__ = [
    "StageDriver",
    "RoundCountable",
    "TerminationStrategy",
    "AllPass",
    "BoardSatisfied",
    "Drained",
    "MaxRounds",
    "TerminationPolicy",
    "TerminationReason",
    "has_durable_events",
]


class StageDriver(Generic[E]):
    """Shared streaming lifecycle for the three stage coordination primitives.

    Call :meth:`run` to stream events. Subclasses implement :meth:`_rounds`
    (the round loop) and :meth:`_completed` (terminal event factory), and set
    ``self._reason`` to signal why the loop ended.

    Subclasses with per-unit budgeting set ``self._budget`` and wrap each unit
    of work in :meth:`_run_enveloped` instead of inlining reserve/commit calls.
    """

    def __init__(self) -> None:
        self._reason: TerminationReason | None = None
        self._budget: BudgetLedger | None = None
        """Optional shared ledger — set by Blackboard and WorkQueue (per-unit
        reserve/commit), left None by Ensemble (per-turn check/commit)."""

    async def run(self) -> AsyncGenerator[E, None]:
        """Stream intermediate events from :meth:`_rounds`, then one terminal
        event from :meth:`_completed`. Crashes become ``error`` terminal events."""
        try:
            async for event in self._rounds():
                yield event
            reason = self._reason
            if reason is None:
                reason = TerminationReason(
                    reason="unknown",
                    detail=(
                        f"{type(self).__name__} exited its loop without setting "
                        "a termination reason"
                    ),
                )
            yield self._completed(reason)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 — surface as a failed terminal event, don't crash the stream
            logger.exception("[%s] pipeline crashed: %s", type(self).__name__, exc)
            yield self._completed(
                TerminationReason(
                    reason="error",
                    detail=f"{type(exc).__name__}: {exc}",
                    by_hard_cap=False,
                )
            )

    async def _rounds(self) -> AsyncGenerator[E, None]:
        """Round loop — yield intermediate events, set ``self._reason`` before
        returning. Subclasses must override."""
        raise NotImplementedError
        yield  # pragma: no cover — makes the stub an async generator for typing

    def _completed(self, reason: TerminationReason) -> E:
        """Build the terminal Completed event for ``reason``. Subclasses override."""
        raise NotImplementedError

    async def _run_enveloped(
        self,
        member: str,
        act: Callable[[], Awaitable[tuple[int, float] | None]],
    ) -> tuple[int, float] | None:
        """Reserve → act → commit for one unit of work against ``self._budget``.

        A stage unit is always **one turn slot** (an expert's contribution, a
        worker's claim), so ``act`` returns ``(tokens, cost_usd)`` — or
        ``None`` ("no claim, no contribution") — and this wrapper pins the
        turn count at 1 before delegating to the kernel's settlement envelope
        (:func:`prodagent.kernel.budget.run_enveloped`), where the
        reserve/crash-commits-don't-release invariants live once for every
        spender. Spawn delegates to the same envelope with the child's real
        turn count, so the policy cannot drift between them.

        A member that can't reserve (over cap) never acts — ``None`` comes
        back with no budget movement. The exception propagates to the caller,
        which decides how to isolate the member.
        """

        async def _unit_act() -> tuple[int, int, float] | None:
            unit = await act()
            return None if unit is None else (1, unit[0], unit[1])

        settled = await run_enveloped(self._budget, member=member, act=_unit_act)
        return None if settled is None else (settled[1], settled[2])

    async def _dispatch(
        self,
        activation: Activation,
        run_one: Callable[[str], Awaitable[Any | None]],
        *,
        lock_store: LockStore | None = None,
        lock_scope: str = "",
    ) -> list[tuple[str, Any | None]]:
        """Run one :class:`~prodagent.ports.activation.Activation`
        per its dispatch mode, returning ``(member, result)`` in member order.

        - ``serial`` — one ``run_one`` at a time, in order.
        - ``concurrent`` — all at once via gather (results keep member order,
          so streams stay deterministic even when completion isn't). A raising
          member fails the batch fast **and cancels its in-flight siblings** —
          plain gather would leave them running as orphans whose spend never
          reaches the ledger ("run over, work still burning").
        - ``single_winner`` — candidates race for one lock on ``lock_store``
          (required for this mode); only the winner's ``run_one`` runs. The
          race and the compute are two phases on purpose: the lock is held
          across the whole dispatch, so a candidate whose ``run_one`` never
          suspends can't release before losers even get scheduled — every
          loser's one-shot ``acquire(timeout=0)`` sees it held. This is the
          buzz-in semantics, lifted from Blackboard so any primitive can use it.
        """
        if activation.dispatch == "serial":
            return [(m, await run_one(m)) for m in activation.members]
        if activation.dispatch == "concurrent":
            tasks = [asyncio.ensure_future(run_one(m)) for m in activation.members]
            try:
                results = await asyncio.gather(*tasks)
            except BaseException:
                for task in tasks:
                    if not task.done():
                        task.cancel()
                # Reap cancelled/failed siblings so nothing outlives the batch.
                await asyncio.gather(*tasks, return_exceptions=True)
                raise
            return list(zip(activation.members, results, strict=True))

        # single_winner — lock-first-then-compute.
        if lock_store is None:
            raise ValueError(
                f"Activation {activation.why()!r} dispatch='single_winner' requires a lock_store"
            )
        lock_name = (
            f"{lock_scope}:{activation.label}"
            if lock_scope
            else (f"stage:{id(self)}:{activation.label}")
        )
        winner: str | None = None
        token: LockToken | None = None

        async def _race(name: str) -> None:
            nonlocal winner, token
            try:
                # Non-blocking try-acquire — a losing candidate must never
                # begin computing. timeout=0 on InProcessLockStore is a true
                # trylock (see backends/memory/lock.py).
                acquired = await lock_store.acquire(lock_name, timeout=0)
            except TimeoutError:
                return
            winner, token = name, acquired

        await asyncio.gather(*(_race(name) for name in activation.members))

        if winner is None or token is None:
            return [(m, None) for m in activation.members]
        won_token = token
        try:
            result = await run_one(winner)
        finally:
            await lock_store.release(won_token)
        return [(m, result if m == winner else None) for m in activation.members]


# ── Termination — when does a floor/queue/board stop? ──────────────────────────
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
class AllPass:
    """Business strategy: stop when the most recent *completed* round had at
    least one turn and every turn in it was a pass (member chose not to
    speak) — "the floor has nothing left to say".

    Duck-types on ``store.transcript`` (the floor satisfies it); stores
    without a transcript get no verdict. Compose into
    ``TerminationPolicy(business=AllPass(), hard_cap=...)`` — without it, an
    all-pass ensemble burns rounds until the cap."""

    min_turns: int = 1
    """A round with fewer turns than this doesn't count as a verdict — lets a
    speaking order warm up before pass-silence can stop the floor."""

    def should_stop(
        self, store: RoundCountable, *, next_round: int
    ) -> tuple[bool, TerminationReason | None]:
        transcript = getattr(store, "transcript", None)
        if not transcript:
            return False, None
        last_round = store.round_count() - 1
        turns = [t for t in transcript if t.round == last_round]
        if len(turns) < self.min_turns or not all(t.is_pass() for t in turns):
            return False, None
        return True, TerminationReason(
            reason="convergence",
            detail=f"All of round {last_round}'s {len(turns)} turn(s) were passes — floor converged",
        )


@dataclass
class BoardSatisfied:
    """Business strategy: stop when a predicate over the store holds — the
    composable form of ``BlackboardSpec.terminal_check``, lifted into the
    termination policy so it can OR with other business strategies."""

    check: Callable[[Any], bool]

    def should_stop(
        self, store: RoundCountable, *, next_round: int
    ) -> tuple[bool, TerminationReason | None]:
        if not self.check(store):
            return False, None
        return True, TerminationReason(
            reason="convergence", detail="Board satisfied terminal_check"
        )


@dataclass
class Drained:
    """Business strategy: stop when the store reports drained (no pending,
    no claimed) — the composable form of the work queue's natural stop, so a
    custom store can gain the same semantics in the policy layer."""

    def should_stop(
        self, store: RoundCountable, *, next_round: int
    ) -> tuple[bool, TerminationReason | None]:
        is_drained = getattr(store, "is_drained", None)
        if not callable(is_drained) or not is_drained():
            return False, None
        return True, TerminationReason(
            reason="convergence", detail="Store drained — no pending or claimed items"
        )


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


async def has_durable_events(spec: Any) -> bool:
    """True when the spec's durable projection already has events under
    ``run_id`` — the restore-or-fresh decision every durable stage makes
    before its first round (restore when true, fresh start when false)."""
    if spec.event_log is None or not spec.run_id:
        return False
    return bool(await spec.event_log.get_events(spec.run_id))
