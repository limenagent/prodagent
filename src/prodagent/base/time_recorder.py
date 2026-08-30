"""RecordingTimePort — clock asks inside a run become boundary facts.

The time half of the fact pipeline: code within a run asks the clock
through the determinism port; when a run is being recorded, each answer is
buffered and flushed to the run's boundary stream at the driver's turn
boundaries (``{"port": wall|monotonic, "value": …}``). The cassette
derives ``kind="clock"`` records from them — sparse by construction,
because they appear only where the run actually asked.

Buffer-then-flush is forced by shape: the port's methods are synchronous
(and must stay so — a clock that awaits is not a clock), so the driver
owns the flush points. Buffer order is call order, which is exactly the
order the frozen clock will replay. Reads outside any run scope pass
through unbuffered (background work is not a fact of a run).
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from prodagent.base.determinism import current_time
from prodagent.base.event_log import BoundaryEventType, Event, boundary_stream
from prodagent.base.run_context import current_event_log, current_run_id

if TYPE_CHECKING:
    from prodagent.base.determinism import TimePort
    from prodagent.ports.observability import EventLog

logger = logging.getLogger(__name__)

__all__ = ["RecordingTimePort"]


class RecordingTimePort:
    """A ``TimePort`` that answers from the installed clock and buffers the
    answer for the run's boundary stream.

    Delegates to whatever port was current at construction, so a frozen
    clock underneath (or none) composes without either knowing."""

    def __init__(self, inner: TimePort | None = None) -> None:
        self._inner = inner or current_time()
        self._buffer: list[tuple[str, float]] = []
        self._flushing = False

    def wall(self) -> float:
        value = self._inner.wall()
        self._note("wall", value)
        return value

    def monotonic(self) -> float:
        value = self._inner.monotonic()
        self._note("monotonic", value)
        return value

    def _note(self, port_name: str, value: float) -> None:
        if self._flushing:
            # Building the fact events asks the clock for their timestamps —
            # recording those would feed the buffer we are draining (and a
            # comprehension over a self-appending list never ends).
            return
        log = current_event_log()
        run_id = current_run_id()
        if log is None or run_id is None:
            return  # outside any run — the reading is not a fact of a run
        self._buffer.append((port_name, value))

    async def flush(self, event_log: EventLog | None = None, run_id: str | None = None) -> int:
        """Append every buffered reading, in call order, to the boundary
        stream. Idempotent per buffer — the driver calls it at turn
        boundaries and before terminal markers, so clock facts always land
        ahead of the marker they precede."""
        log = event_log or current_event_log()
        resolved_run = run_id or current_run_id()
        if not self._buffer:
            return 0
        if log is None or resolved_run is None:
            self._buffer = []  # the run ended outside any scope; drop, don't stall
            return 0
        # Swap the buffer out BEFORE building events: Event.make stamps a
        # timestamp by asking this very port, and a self-appending buffer
        # under iteration is an infinite loop.
        buffered = self._buffer
        self._buffer = []
        self._flushing = True
        try:
            events = [
                Event.make(
                    BoundaryEventType.CLOCK_RECORDED,
                    stream_id=boundary_stream(resolved_run),
                    version=0,
                    port=port_name,
                    value=value,
                )
                for port_name, value in buffered
            ]
        finally:
            self._flushing = False
        try:
            await log.append_events(events)
        except Exception:  # noqa: BLE001 — recording must never break the clock
            logger.exception("[boundary] failed to flush clock facts for %s", resolved_run)
        return len(events)

    def pending(self) -> int:
        return len(self._buffer)
