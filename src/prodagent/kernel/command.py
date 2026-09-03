"""Commands — what a node's return asks the kernel to do (column 11).

A command is an *intent*, not a fact: the kernel checks it (the target
exists, the reducer is declared) before anything lands in the event log.
Two live commands:

- **Update(key, value, reducer)** — merge into the run's shared state.
  Two nodes writing one key without a reducer is a conflict the gate
  rejects; with one ("last", "add", "append", "merge"), the merge is
  deterministic and replayable.
- **Goto(target)** — requeue a node at runtime (column 6's "choose the
  edge at runtime"): the named completed node goes PENDING and joins the
  next wave. Structure still comes from the graph (back edges carry the
  loops you can draw statically); goto is for the directions only the
  running data — or the model — can pick.

Because commands are frozen, serializable data, every application lands
in the event log, replays in the fold, and audits — dynamic control
without breaking "state is the fold of events".
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

__all__ = [
    "Command",
    "Update",
    "Goto",
    "Send",
    "WAIT",
    "REDUCERS",
    "resolve_reducer_name",
    "command_from_wire",
]


def _reduce_last(_old: Any, new: Any) -> Any:
    return new


def _reduce_add(old: Any, new: Any) -> Any:
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
    "add": _reduce_add,
    "append": _reduce_append,
    "merge": _reduce_merge,
}
"""Reducers by name — a command stays serializable; the merge semantics
resolve through this table on both live runs and fold replays.

The accumulate rule's wire name is ``add`` (column 7's vocabulary); the
legacy ``sum`` spelling still reads — old checkpoints and replayed events
predate the rename and must fold, not crash."""

_LEGACY_REDUCERS = {"sum": "add"}


def resolve_reducer_name(name: str) -> str:
    """Canonical wire name for a reducer — the accumulate rule writes
    ``add``; the legacy ``sum`` spelling reads (old checkpoints and
    replayed events predate the rename and must fold, not crash)."""
    return _LEGACY_REDUCERS.get(name, name)


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


@dataclass(frozen=True)
class Goto(Command):
    """Requeue a target at runtime (column 6): put the named node back into
    the next wave's ready set.

    Goto changes no call stack and jumps to no code line — it is purely the
    scheduler's requeue: a completed node goes PENDING again and re-runs.
    The one thing it cannot do is invent a node: the target must exist in
    the plan (the optional ``targets`` declaration on a step lets compilers
    check this statically; the scheduler checks it for real).

    One reserved target: ``"__wait__"`` (column 17) — requeue *the sender*
    for one wave, the join node's "not all results are in yet, look again
    next pass" idiom."""

    target: str

    def to_wire(self) -> dict[str, Any]:
        return {"goto": {"target": self.target}}


WAIT = "__wait__"
"""Reserved goto target: sleep one wave and re-run me (the join idiom)."""


@dataclass(frozen=True)
class Send(Command):
    """Instantiate one copy of a template node (column 17's dynamic
    fan-out): the count is runtime data, so the sender returns one Send per
    item and the scheduler materializes each as a fresh node instance.

    Instances inherit the template's body; ``payload`` becomes the
    instance's params (resolved like any node's). They join the next wave
    root-ready; their writes fold through the same channels — a merge
    channel keyed by the item is how N results land without overwriting.
    The wave's readonly bound (and the wave cap) is the concurrency limit;
    a node throttling its own fan brings its own semaphore."""

    template: str
    payload: dict[str, Any]

    def to_wire(self) -> dict[str, Any]:
        return {"send": {"template": self.template, "payload": dict(self.payload)}}


def command_from_wire(d: dict[str, Any]) -> Command | None:
    """Rebuild a command from its durable form; dict markers a plain
    function returned (``{"update": {...}}`` / ``{"goto": ...}``) land here
    too — no framework types required of the fn author."""
    if (update := d.get("update")) is not None:
        reducer = update.get("reducer")
        if reducer is not None:
            reducer = resolve_reducer_name(str(reducer))
        return Update(
            key=str(update.get("key", "")),
            value=update.get("value"),
            reducer=reducer,
        )
    if (goto := d.get("goto")) is not None:
        target = goto.get("target") if isinstance(goto, dict) else goto
        if target:
            return Goto(target=str(target))
    if (send := d.get("send")) is not None and isinstance(send, dict) and send.get("template"):
        return Send(template=str(send["template"]), payload=dict(send.get("payload") or {}))
    return None
