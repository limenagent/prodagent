"""Session port — durable conversation root for cross-turn chat."""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol, runtime_checkable

if TYPE_CHECKING:
    from prodagent.base.session import ConversationSession


@runtime_checkable
class SessionStore(Protocol):
    """Durable home for ``ConversationSession``.

    Same optimistic-concurrency API shape as ``CheckpointStore``/``EventLog``,
    but a different scope: a session spans many runs (one turn per
    ``AgentRun``), while ``CheckpointStore``/``EventLog`` only ever track
    state *within* a single run. Peers, not layers — neither wraps the
    other.
    """

    async def save(self, session: ConversationSession, expected_version: int | None = None) -> None:
        """Idempotent atomic persist under ``session.session_id``.

        ``expected_version`` enables optimistic concurrency: raise
        ``VersionConflict`` if the stored version differs.
        """
        ...

    async def load(self, session_id: str) -> ConversationSession | None:
        """Return the session for ``session_id``, or ``None`` if absent."""
        ...
