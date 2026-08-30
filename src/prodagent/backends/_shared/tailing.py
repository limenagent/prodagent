"""Subscribe-side machinery shared by every ``EventLog`` backend.

``subscribe`` must satisfy the suffix law: a subscriber sees exactly the
suffix ``seq > since_seq`` of its stream — strictly increasing, no
duplicates, no gaps — including events appended after the subscription
started. The mechanics that guarantee that are identical across backends,
so they live here: an in-process wake registry (appends poke waiters, so
the common single-process case pays no poll latency) plus a
poll-interval fallback that catches writers in *other* processes — a wake
registered after the poke is late by at most one interval, never wrong.
"""

from __future__ import annotations

import asyncio
import contextlib
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Awaitable, Callable

    from prodagent.base.event_log import Event

__all__ = ["POLL_INTERVAL_SECONDS", "StreamWakes", "tail_stream"]

POLL_INTERVAL_SECONDS = 0.05
"""Fallback poll cadence for cross-process writers. Correctness never
depends on it (wakes cover the in-process writer); only tail latency does."""


class StreamWakes:
    """Per-stream wake events — ``notify`` on the write path, ``wait`` on
    the subscribe path."""

    def __init__(self) -> None:
        self._waiters: dict[str, list[asyncio.Event]] = {}

    def notify(self, stream_id: str) -> None:
        """Wake every current waiter for ``stream_id`` (and forget them —
        a woken waiter re-fetches, so the event is single-shot)."""
        for evt in self._waiters.pop(stream_id, []):
            evt.set()

    async def wait(self, stream_id: str, timeout: float) -> None:
        """Sleep until ``notify(stream_id)`` or ``timeout`` seconds pass.

        A notify that fires *before* this call registers is missed by
        design — the poll fallback catches it one interval later. That
        trades a worst-case interval of latency for zero coordination."""
        evt = asyncio.Event()
        waiters = self._waiters.setdefault(stream_id, [])
        waiters.append(evt)
        try:
            await asyncio.wait_for(evt.wait(), timeout)
        except TimeoutError:
            pass
        finally:
            with contextlib.suppress(ValueError):
                waiters.remove(evt)  # notify() may already have popped it — the wake fired


async def tail_stream(
    fetch: Callable[[str, int], Awaitable[list[Event]]],
    wakes: StreamWakes,
    stream_id: str,
    since_seq: int,
    *,
    poll_interval: float = POLL_INTERVAL_SECONDS,
) -> AsyncIterator[Event]:
    """The shared subscribe loop: fetch-and-yield the new suffix, then sleep
    until poked (or the poll interval lapses), forever.

    ``fetch`` is the backend's ``get_after`` — the same method recovery
    uses, so subscribe and recovery can never disagree about what the
    suffix is. Advancing ``since_seq`` only through yielded events is what
    makes the no-dup/no-gap property local to this loop."""
    while True:
        for event in await fetch(stream_id, since_seq):
            yield event
            since_seq = event.seq
        await wakes.wait(stream_id, poll_interval)
