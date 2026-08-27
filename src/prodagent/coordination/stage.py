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
a :class:`~prodagent.kernel.budget.BudgetLedger` — is the
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

from prodagent.coordination.termination import TerminationReason
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
