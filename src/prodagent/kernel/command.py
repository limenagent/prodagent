"""Update — the state-write primitive a node returns.

Control flow used to ride home as commands too (Goto jump-backs, Send
fan-outs). It now lives where it belongs: structure is declared with the
combinators (``Loop`` iterates, ``Parallel`` fans out and joins — the join
IS the barrier, a readiness rule, not a command), and *data-driven*
control — which branch, whether to stop — is expressed by writing shared
state here and letting Route selectors and Loop predicates read it. What
remains a command is the pure data write:

- **Update(key, value, reducer)** — merge into the run's shared state.
  Two nodes writing one key without a reducer is a conflict the gate
  rejects; with one ("last", "sum", "append", "merge"), the merge is
  deterministic and replayable.

Because it is frozen, serializable data, every application lands in the
event log, replays in the fold, and audits — dynamic state without
breaking "state is the fold of events".
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

__all__ = ["Command", "Update", "REDUCERS", "command_from_wire"]


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
    """Base marker: a node's return value that writes state."""

    def to_wire(self) -> dict[str, Any]:
        raise NotImplementedError

    @classmethod
    def kind(cls) -> str:
        return cls.__name__.lower()


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
    function returned (``{"update": {...}}``) land here too — no framework
    types required of the fn author."""
    if (update := d.get("update")) is not None:
        return Update(
            key=str(update.get("key", "")),
            value=update.get("value"),
            reducer=update.get("reducer"),
        )
    return None
