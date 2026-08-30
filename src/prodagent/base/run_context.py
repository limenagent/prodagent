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

__all__ = ["current_event_log", "current_run_id", "run_scope"]

_run_id: ContextVar[str | None] = ContextVar("prodagent_run_id", default=None)
_event_log: ContextVar[EventLog | None] = ContextVar("prodagent_event_log", default=None)


def current_run_id() -> str | None:
    """The run executing on this task, or ``None`` outside any run scope
    (background jobs like skill distillation — recorders skip those)."""
    return _run_id.get()


def current_event_log() -> EventLog | None:
    """The WAL of the run executing on this task — observers that live above
    the drivers (the span recorder) reach the fact pipeline through here
    instead of holding a store of their own."""
    return _event_log.get()


@contextmanager
def run_scope(run_id: str, event_log: EventLog | None = None) -> Iterator[None]:
    """Attribute everything awaited inside the block to ``run_id`` (and,
    when given, expose the run's WAL to observers on this task).

    Drivers open one scope around the run they drive; nested scopes
    (a child activation inside a parent's stream) shadow correctly because
    each driver opens its own before yielding control.
    """
    id_token = _run_id.set(run_id)
    log_token = _event_log.set(event_log)
    try:
        yield
    finally:
        _event_log.reset(log_token)
        _run_id.reset(id_token)
