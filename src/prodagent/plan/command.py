"""Command — dynamic control flow as data (column 9).

Jump, fan-out, and state-merge are not scheduler syntax: they are values a
node returns. Because they are frozen, serializable data, they can be
recorded in the event log, replayed by the fold, and audited — dynamic
control flow without breaking "state is the fold of events".

Three commands, one rule each:

- **Goto(target)** — light the target node up: a runtime edge on top of
  the static graph. Looping back to a COMPLETED node resets it (the one
  legal exit from a terminal node state, because the graph *asked* for a
  redo). How many times a goto may orbit is policy (the dead-loop guard),
  not mechanism.
- **Send(template, items, key)** — instantiate the template node once per
  item and re-wire every downstream edge of the *sending* node onto the
  whole batch: the map-reduce pattern. Downstream nodes wait for all
  instances — that join IS the barrier (column 9: barrier is a readiness
  rule, not a fourth command).
- **Update(key, value, reducer)** — merge into the run's shared state.
  Two nodes writing one key without a reducer is a conflict the gate
  rejects; with one ("last", "sum", "append", "merge"), the merge is
  deterministic and replayable.

Runtime gates (column 9's "动态不等于无政府"): a Goto target must exist;
a Send fan-out is capped; an Update without a reducer on a contested key
fails. Every application lands in the event log.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

__all__ = ["Command", "Goto", "Send", "Update", "REDUCERS", "command_from_wire"]

_MAX_FANOUT = 16


def _reduce_last(_old: Any, new: Any) -> Any:
    return new


def _reduce_sum(old: Any, new: Any) -> Any:
    return old + new


def _reduce_append(old: Any, new: Any) -> list[Any]:
    return [*(_as_list(old)), *(_as_list(new))]


def _reduce_merge(old: Any, new: Any) -> Any:
    return {**(_as_dict(old)), **(_as_dict(new))}


def _as_list(v: Any) -> list[Any]:
    return v if isinstance(v, list) else [v]


def _as_dict(v: Any) -> dict[Any, Any]:
    return v if isinstance(v, dict) else {}


REDUCERS: dict[str, Any] = {
    "last": _reduce_last,
    "sum": _reduce_sum,
    "append": _reduce_append,
    "merge": _reduce_merge,
}
"""Reducers by name — a command stays serializable; the merge semantics
resolve through this table on both live runs and fold replays."""


@dataclass(frozen=True)
class Command:
    """Base marker: a node's return value that changes what runs next."""

    def to_wire(self) -> dict[str, Any]:
        raise NotImplementedError

    @classmethod
    def kind(cls) -> str:
        return cls.__name__.lower()


@dataclass(frozen=True)
class Goto(Command):
    """Light ``target`` up — a runtime edge onto the static graph."""

    target: str

    def to_wire(self) -> dict[str, Any]:
        return {"goto": self.target}


@dataclass(frozen=True)
class Send(Command):
    """Instantiate ``template`` once per item — map; downstream re-wires
    onto the whole batch — the join is the barrier."""

    template: str
    items: tuple[Any, ...]
    key: str = ""

    def __post_init__(self) -> None:
        if len(self.items) > _MAX_FANOUT:
            raise ValueError(
                f"Send fan-out {len(self.items)} exceeds the cap of {_MAX_FANOUT} — "
                "batch the work inside one node instead"
            )

    def instance_ids(self) -> list[str]:
        key = self.key or self.template
        return [f"{key}#{i}" for i in range(len(self.items))]

    def to_wire(self) -> dict[str, Any]:
        return {"send": {"template": self.template, "items": list(self.items), "key": self.key}}


@dataclass(frozen=True)
class Update(Command):
    """Merge into the run's shared state under ``reducer``'s rule."""

    key: str
    value: Any
    reducer: str | None = None

    def to_wire(self) -> dict[str, Any]:
        return {"update": {"key": self.key, "value": self.value, "reducer": self.reducer}}


def command_from_wire(d: dict[str, Any]) -> Command | None:
    """Rebuild a command from its durable form; dict markers a plain
    function returned (``{"goto": ...}`` etc.) land here too — no framework
    types required of the fn author."""
    if (target := d.get("goto")) is not None:
        return Goto(target=str(target))
    if (send := d.get("send")) is not None:
        return Send(
            template=str(send.get("template", "")),
            items=tuple(send.get("items") or ()),
            key=str(send.get("key", "")),
        )
    if (update := d.get("update")) is not None:
        return Update(
            key=str(update.get("key", "")),
            value=update.get("value"),
            reducer=update.get("reducer"),
        )
    return None
