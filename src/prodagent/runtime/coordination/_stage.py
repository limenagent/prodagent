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

Subclasses plug in via :meth:`_rounds` (the loop; yields intermediate events,
sets ``self._reason`` before returning) and :meth:`_completed` (the terminal
event factory). The base owns the crash guard and finalization, so a fix to
either applies to all three primitives at once.
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, Generic, TypeVar

from prodagent.runtime.coordination.termination import TerminationReason

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator

logger = logging.getLogger(__name__)

E = TypeVar("E")

__all__ = ["StageDriver"]


class StageDriver(Generic[E]):
    """Shared streaming lifecycle for the three stage coordination primitives.

    Call :meth:`run` to stream events. Subclasses implement :meth:`_rounds`
    (the round loop) and :meth:`_completed` (terminal event factory), and set
    ``self._reason`` to signal why the loop ended.
    """

    def __init__(self) -> None:
        self._reason: TerminationReason | None = None

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
