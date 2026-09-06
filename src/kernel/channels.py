"""channels — state channels and reducers (one of the kernel's key ideas).

Why isn't state a plain dict that you update with ``state[k] = v``?
Because several nodes run concurrently within a wave and may write the same
key at the same time:
- a message history should be *appended* to, not overwritten;
- a cost counter should be *added*;
- only something like the current phase is *last write wins*.

So each key (channel) explicitly declares a reducer: (old, new) -> merged.
The scheduler folds with the reducer once at the wave barrier, making the
concurrent result deterministic regardless of which node finishes first.
A reducer must be a pure function — this is also what lets state be rebuilt
by replaying events.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

Reducer = Callable[[Any, Any], Any]


def _last(old: Any, new: Any) -> Any:
    return new


def _append(old: Any, new: Any) -> list:
    base = list(old or [])
    if isinstance(new, list):
        base.extend(new)
    else:
        base.append(new)
    return base


def _add(old: Any, new: Any) -> Any:
    return (old or 0) + (new or 0)


def _merge(old: Any, new: Any) -> dict:
    out = dict(old or {})
    out.update(new or {})
    return out


@dataclass(frozen=True)
class Channel:
    """A state channel: initial value + reducer + identity element.

    ``allow_multi`` says whether multiple nodes may write it in the same wave.
    append/add/merge are associative and commutative, so they allow it; a last
    channel written concurrently would depend on scheduling order — an
    ambiguous write — so it is forbidden by default.

    ``empty`` is the reducer's identity element, used to first aggregate the
    multiple writes to a channel within a wave into one "wave delta". The event
    log records the wave delta, so replay does not double-count.
    """

    init: Any
    reducer: Reducer
    allow_multi: bool = True
    empty: Any = None

    def fold(self, old: Any, new: Any) -> Any:
        return self.reducer(old, new)


# — Factories for the four common channels; the name is the semantics. —
def last(init: Any = None) -> Channel:
    """Last write wins: single values such as the current phase or verdict."""
    return Channel(init, _last, allow_multi=False, empty=None)


def append(init: Any = None) -> Channel:
    """Append into a list: message history, retrieved snippets."""
    return Channel(list(init or []), _append, allow_multi=True, empty=[])


def add(init: Any = 0) -> Channel:
    """Numeric accumulation: cost, counters."""
    return Channel(init, _add, allow_multi=True, empty=0)


def merge(init: Any = None) -> Channel:
    """Dict merge: combining structured results."""
    return Channel(dict(init or {}), _merge, allow_multi=True, empty={})


class AmbiguousWrite(RuntimeError):  # noqa: N818 — the name states the semantics; teaching beats naming convention
    """Several nodes wrote a last channel in the same wave — nondeterministic,
    and must be handled explicitly."""


@dataclass
class _Write:
    key: str
    value: Any
    writer: str  # which node wrote it, so errors can point to the source


class WaveWrites:
    """Collects state deltas from every node in a wave and folds once at the
    barrier.

    Nodes never touch shared state while running; they hand deltas here, which
    removes the race of "reading another node's half-finished state".
    """

    def __init__(self, channels: dict[str, Channel]):
        self._channels = channels
        self._buffer: list[_Write] = []

    def buffer(self, key: str, value: Any, writer: str) -> None:
        channel = self._channels.get(key)
        if channel is None:
            # Writing a channel that was never declared is almost always a typo
            # or a design omission — failing early beats silently dropping it.
            raise KeyError(f"wrote to undeclared state channel {key!r} (from node {writer!r})")
        self._buffer.append(_Write(key, value, writer))

    def check_ambiguous(self) -> None:
        """At the barrier, check whether a last channel got multiple writers."""
        writers_by_key: dict[str, set[str]] = {}
        for w in self._buffer:
            writers_by_key.setdefault(w.key, set()).add(w.writer)
        for key, writers in writers_by_key.items():
            channel = self._channels[key]
            if not channel.allow_multi and len(writers) > 1:
                raise AmbiguousWrite(
                    f"channel {key!r} is last (last-write-wins) but was written concurrently "
                    f"by multiple nodes {writers} in one wave; the result would depend on "
                    f"scheduling order. Use append/add/merge, or let only one node write it."
                )

    def drain(self) -> list[_Write]:
        """Return this wave's deltas in arrival order and clear the buffer."""
        out = self._buffer
        self._buffer = []
        return out
