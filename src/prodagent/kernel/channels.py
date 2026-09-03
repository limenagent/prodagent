"""Channels — named state lanes with a declared merge rule (column 7).

A bare dict loses data the moment two parallel nodes read-modify-write the
same key off one stale snapshot: the later write silently covers the
earlier one, and the result depends on scheduling order. The fix is
structural, not behavioral: State is a set of *named channels*, each
declaring its initial value and its reducer — ``last`` (single-writer
overwrite), ``append`` (list accumulation), ``add`` (numeric accumulation),
or a custom function. Concurrent writes then fold deterministically at the
wave barrier; a merge rule that is not order-independent (``last``) with
more than one writer in a wave is a conflict the kernel reports
(:class:`AmbiguousWrite`), never silently resolves.

Declared channels get the column's strict discipline: writes buffer until
the wave barrier and fold there (same-wave nodes read the wave-start
snapshot), and every folded write lands in the event log as an ``Update``
— so the fold replays. Keys with no declared channel keep the legacy
immediate-apply path: first write lands, a second writer must say how.

Channels are declared on the Plan (the blueprint — the *rules*); the
current values live in ``run.shared`` (the fold *result*). A channel's
``init`` must be serializable (it rides the wire); a custom callable
reducer is process-local by design — the same discipline as an Edge's
``when`` predicate — and serializes as the ``last`` rule with the callable
dropped, so cross-process restores stay honest.
"""

from __future__ import annotations

import copy
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from prodagent.kernel.command import REDUCERS

if TYPE_CHECKING:
    from collections.abc import Callable

__all__ = [
    "AmbiguousWrite",
    "Channel",
    "WaveWrites",
    "add",
    "append",
    "apply_channel_inits",
    "channel_from_wire",
    "last",
    "merge",
]


class AmbiguousWrite(RuntimeError):
    """More than one node wrote a ``last`` channel in the same wave.

    ``last`` is order-dependent (new replaces old), so two same-wave
    writers make the result depend on scheduling order — the kernel
    refuses to guess. Fix it one of three ways: switch the channel to a
    order-independent rule (``append``/``add``/a commutative custom one),
    give the writers separate channels, or restructure so only one node
    writes (a conditional edge guaranteeing a single reachable branch)."""

    def __init__(self, key: str, writers: list[str]) -> None:
        self.key = key
        self.writers = writers
        super().__init__(
            f"channel {key!r}: same-wave writers {writers} on a 'last' rule — "
            "the merge is order-dependent; declare append/add (or a "
            "commutative custom reducer), split the channel, or guarantee "
            "a single writer"
        )


@dataclass(frozen=True, slots=True)
class Channel:
    """One named lane of run state: an initial value plus a merge rule.

    ``reducer`` is a wire name from :data:`REDUCERS` (``last`` is the
    default single-writer rule); ``reduce`` optionally carries a live
    custom function that overrides the named rule in-process.
    """

    init: Any = None
    reducer: str = "last"
    reduce: Callable[[Any, Any], Any] | None = None

    def __post_init__(self) -> None:
        if self.reducer not in REDUCERS:
            raise ValueError(
                f"channel reducer {self.reducer!r} is not a declared rule "
                f"(declared: {sorted(REDUCERS)})"
            )

    @property
    def is_order_independent(self) -> bool:
        """True when merged results do not depend on write order.

        ``last`` replaces, so it is order-dependent — the one rule that
        demands a single writer per wave."""
        return self.reducer != "last"

    def resolve(self) -> Callable[[Any, Any], Any]:
        """The merge function this channel folds with."""
        return self.reduce if self.reduce is not None else REDUCERS[self.reducer]

    def to_wire(self) -> dict[str, Any]:
        # A custom callable is process-local (like an Edge ``when``): the
        # wire keeps the named rule's slot honest by falling back to last.
        return {"init": self.init, "reducer": self.reducer if self.reduce is None else "last"}


def channel_from_wire(d: dict[str, Any]) -> Channel:
    """Rebuild a channel from its wire form (init + rule name)."""
    return Channel(init=d.get("init"), reducer=str(d.get("reducer", "last") or "last"))


def last(init: Any = None) -> Channel:
    """Single-writer overwrite: the new value replaces the old."""
    return Channel(init=init, reducer="last")


def append(init: Any = None) -> Channel:
    """List accumulation: everyone's write survives, order-free."""
    return Channel(init=[] if init is None else init, reducer="append")


def add(init: Any = 0) -> Channel:
    """Numeric accumulation: values sum."""
    return Channel(init=init, reducer="add")


def merge(init: Any = None) -> Channel:
    """Dict accumulation: keys merge — the natural fan-out channel, where
    N instances each write their own key of one results lane."""
    return Channel(init={} if init is None else init, reducer="merge")


def apply_channel_inits(channels: dict[str, Channel], shared: dict[str, Any]) -> None:
    """Seed a run's shared state with each channel's init.

    ``setdefault`` semantics: a resumed run already carries folded values,
    and the init must not clobber them. Inits are deep-copied per run — a
    mutable default (a list) shared across runs would be the classic trap."""
    for name, channel in channels.items():
        shared.setdefault(name, copy.deepcopy(channel.init))


class WaveWrites:
    """The per-wave write buffer for declared channels (column 7's barrier).

    Writes to declared channels land here at node completion instead of
    touching ``run.shared`` immediately; the scheduler folds the whole
    buffer at the wave barrier (checking :class:`AmbiguousWrite` first),
    which is what makes same-wave nodes all read the wave-start snapshot.
    Order-independence of append/add is what makes the fold's result
    independent of completion order."""

    def __init__(self, channels: dict[str, Channel] | None = None) -> None:
        self._channels = channels if channels is not None else {}
        self._writes: dict[str, list[tuple[str, Any]]] = {}

    @property
    def channels(self) -> dict[str, Channel]:
        return self._channels

    @channels.setter
    def channels(self, channels: dict[str, Channel]) -> None:
        self._channels = channels

    def is_declared(self, key: str) -> bool:
        return key in self._channels

    def buffer(self, key: str, value: Any, writer: str) -> None:
        """Record one node's write to a declared channel."""
        self._writes.setdefault(key, []).append((writer, value))

    def __bool__(self) -> bool:
        return bool(self._writes)

    def drain(self) -> list[tuple[str, Any, str]]:
        """Empty the buffer as (key, value, writer) rows in arrival order."""
        rows = [(key, value, writer) for key, ws in self._writes.items() for writer, value in ws]
        self._writes.clear()
        return rows

    def check_ambiguous(self) -> None:
        """Fail closed on same-wave multi-writers to an order-dependent rule.

        Runs before the fold: the conflict is a blueprint-level mistake and
        must abort the wave, not produce an order-dependent merge."""
        for key, ws in self._writes.items():
            if len(ws) > 1 and not self._channels[key].is_order_independent:
                raise AmbiguousWrite(key, [writer for writer, _ in ws])
