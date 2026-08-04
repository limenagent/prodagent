"""EventLog port — append-only event log for event-sourced recovery."""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol, runtime_checkable

if TYPE_CHECKING:
    from prodagent.core.event_log import Event


@runtime_checkable
class EventLog(Protocol):
    """Append-only event log — the second half of event-sourced recovery.

    Capabilities:
      BASE (required): append, get_events, get_after
    """

    async def append(self, event: Event, expected_seq: int | None = None) -> int:
        """Assign a monotonic LSN, persist, return the seq.

        ``expected_seq`` enables optimistic concurrency: raise
        ``VersionConflict`` if the stored tail seq differs.
        """
        ...

    async def get_events(self, plan_id: str) -> list[Event]:
        """Events for ``plan_id`` in append order."""
        ...

    async def get_after(self, plan_id: str, since_seq: int) -> list[Event]:
        """Events for ``plan_id`` with ``seq > since_seq`` (exact tail replay)."""
        ...
