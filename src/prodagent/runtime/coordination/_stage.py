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
lifecycle *around* the loop:

- a raise out of the round loop becomes a terminal ``error`` event instead of
  killing the stream (one member/expert/worker blowing up must not take the run
  with it — and each primitive already isolates per-unit failures *inside* the
  loop; this guard is the backstop for anything that escapes that);
- a loop that ends without setting a reason finalizes to ``unknown`` rather than
  emitting a reasonless terminal event.

What is also identical — for the two primitives whose units of work run through
a :class:`~prodagent.runtime.coordination.budget_ledger.BudgetLedger` — is the
*envelope* around each unit: reserve a turn → run the act → commit the actual
cost (or release the reservation if the act crashed before spending anything).
That envelope lives in :meth:`_run_enveloped`; only the act itself differs per
primitive (an expert's ``try_contribute``, a worker's ``try_claim_and_run``).

Subclasses plug in via :meth:`_rounds` (the loop; yields intermediate events,
sets ``self._reason`` before returning) and :meth:`_completed` (the terminal
event factory). The base owns the crash guard, finalization, and the budget
envelope, so a fix to any of the three applies to all primitives at once.
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, Any, Generic, TypeVar

from prodagent.core.exceptions import BudgetExceeded
from prodagent.runtime.coordination.activation import Activation
from prodagent.runtime.coordination.termination import TerminationReason

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator, Awaitable, Callable

    from prodagent.ports.lock import LockStore, LockToken
    from prodagent.runtime.coordination.activation import Activation
    from prodagent.runtime.coordination.budget_ledger import BudgetLedger

logger = logging.getLogger(__name__)

E = TypeVar("E")

__all__ = ["StageDriver"]


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
    ) -> Any | None:
        """Reserve → act → commit for one unit of work against ``self._budget``.

        ``act`` runs the unit and returns either ``None`` ("nothing happened —
        no claim, no contribution") or ``(tokens, cost_usd)`` — the actuals to
        commit against the reserved turn. The reservation is reconciled away by
        the commit; if ``act`` *raises*, the reservation is released instead
        (the crashed attempt doesn't consume a turn — a retry re-charges it
        then) and the exception propagates to the caller, which decides how to
        isolate the member. A member that can't reserve (over cap) never acts —
        ``None`` comes back with no budget movement.

        This is the one envelope Blackboard and WorkQueue share; hoisting it
        here means the reserve/release/commit invariants live once.
        """
        if self._budget is not None:
            try:
                await self._budget.reserve(member=member, turns=1)
            except BudgetExceeded:
                return None
        try:
            actuals = await act()
        except asyncio.CancelledError:
            raise
        except Exception:
            if self._budget is not None:
                await self._budget.release(member=member, reserved_turns=1)
            raise
        if self._budget is not None:
            tokens, cost_usd = actuals if actuals is not None else (0, 0.0)
            await self._budget.commit(
                member=member,
                turns=1,
                tokens=tokens,
                cost_usd=cost_usd,
                reserved_turns=1,
            )
        return actuals

    async def _dispatch(
        self,
        activation: Activation,
        run_one: Callable[[str], Awaitable[Any | None]],
        *,
        lock_store: LockStore | None = None,
        lock_scope: str = "",
    ) -> list[tuple[str, Any | None]]:
        """Run one :class:`~prodagent.runtime.coordination.activation.Activation`
        per its dispatch mode, returning ``(member, result)`` in member order.

        - ``serial`` — one ``run_one`` at a time, in order.
        - ``concurrent`` — all at once via gather (results keep member order,
          so streams stay deterministic even when completion isn't).
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
            results = await asyncio.gather(*(run_one(m) for m in activation.members))
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
