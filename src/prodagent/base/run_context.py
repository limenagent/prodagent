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

__all__ = ["current_run_id", "run_scope"]

_run_id: ContextVar[str | None] = ContextVar("prodagent_run_id", default=None)


def current_run_id() -> str | None:
    """The run executing on this task, or ``None`` outside any run scope
    (background jobs like skill distillation — recorders skip those)."""
    return _run_id.get()


@contextmanager
def run_scope(run_id: str) -> Iterator[None]:
    """Attribute everything awaited inside the block to ``run_id``.

    Drivers open one scope around the run they drive; nested scopes
    (a child activation inside a parent's stream) shadow correctly because
    each driver opens its own before yielding control.
    """
    token = _run_id.set(run_id)
    try:
        yield
    finally:
        _run_id.reset(token)
