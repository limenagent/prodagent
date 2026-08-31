"""Run-identity context — which run is executing on this async task.

The boundary recorder (``llm/recording.py``) needs the run's identity to
route its facts to the right stream; the LLM client it wraps is built once
per agent and shared by every run, so the identity cannot ride the client
and must not leak into the port's signature. A contextvar carries it — the
same discipline as ``base.determinism``: per-async-task isolation for free
(a child agent driven in its own task opens its own scope), token reset on
exit so a crashed run cannot leave its identity behind in the surrounding
context.
"""

from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Iterator

    from prodagent.ports.observability import EventLog

__all__ = [
    "current_event_log",
    "current_run_id",
    "current_tape_root",
    "run_scope",
    "tape_root_scope",
]

_run_id: ContextVar[str | None] = ContextVar("prodagent_run_id", default=None)
_event_log: ContextVar[EventLog | None] = ContextVar("prodagent_event_log", default=None)
_tape_root: ContextVar[str | None] = ContextVar("prodagent_tape_root", default=None)


def current_run_id() -> str | None:
    """The run executing on this task, or ``None`` outside any run scope
    (background jobs like skill distillation — recorders skip those)."""
    return _run_id.get()


def current_event_log() -> EventLog | None:
    """The WAL of the run executing on this task — observers that live above
    the drivers (the span recorder) reach the fact pipeline through here
    instead of holding a store of their own."""
    return _event_log.get()


def current_tape_root() -> str | None:
    """The root run this task's work belongs to, for tape attribution.

    A multi-agent orchestration opens one root scope; every member turn
    started inside it prefixes its run id with ``<root>::`` (the same
    convention spawned children already follow), so one tape in the catalog
    holds the whole multi-agent run. Inherited by nested tasks (contextvars copy
    at task creation) — a member's own tools attribute correctly too."""
    return _tape_root.get()


@contextmanager
def tape_root_scope(root_id: str) -> Iterator[None]:
    """Attribute member runs to one tape root. No-op nested (the outermost
    root wins — a nested orchestration belongs to the big tape)."""
    if current_tape_root() is not None:
        yield
        return
    prev = _tape_root.get()
    _tape_root.set(root_id)
    try:
        yield
    finally:
        if _tape_root.get() == root_id:
            _tape_root.set(prev)


@contextmanager
def run_scope(run_id: str, event_log: EventLog | None = None) -> Iterator[None]:
    """Attribute everything awaited inside the block to ``run_id`` (and,
    when given, expose the run's WAL to observers on this task).

    Drivers open one scope around the run they drive; nested scopes
    (a child activation inside a parent's stream) shadow correctly because
    each driver opens its own before yielding control.
    """
    # Value semantics, not tokens: this scope lives inside async-generator
    # drivers and around ``yield`` — a token cannot reset across Contexts
    # (the consumer may close the generator from another task), values can.
    prev_id, prev_log = _run_id.get(), _event_log.get()
    _run_id.set(run_id)
    _event_log.set(event_log)
    try:
        yield
    finally:
        if _run_id.get() == run_id:
            _run_id.set(prev_id)
        if _event_log.get() is event_log or _event_log.get() == event_log:
            _event_log.set(prev_log)
