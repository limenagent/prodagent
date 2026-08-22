"""In-process session store — the bare-profile default.

Keeps multi-turn ``chat(session_id=...)`` and in-process ``resume=True``
working with zero disk footprint. State dies with the process; cross-restart
durability is the production profile's ``FileSessionStore`` (or Postgres).
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

from prodagent.core.exceptions import VersionConflict

if TYPE_CHECKING:
    from prodagent.core.state.session import ConversationSession

__all__ = ["InMemorySessionStore"]


class InMemorySessionStore:
    """Latest-snapshot session store in process memory.

    Mirrors :class:`~prodagent.backends.file.session_store.FileSessionStore`
    semantics: idempotent save under ``session_id``, optimistic version check,
    monotonic version bump on every save.
    """

    def __init__(self) -> None:
        self._sessions: dict[str, ConversationSession] = {}
        self._versions: dict[str, int] = {}
        self._lock = asyncio.Lock()

    async def save(self, session: ConversationSession, expected_version: int | None = None) -> None:
        async with self._lock:
            stored_version = self._versions.get(session.session_id, 0)
            if expected_version is not None and expected_version != stored_version:
                raise VersionConflict(
                    f"session version mismatch for session={session.session_id}: "
                    f"expected {expected_version}, stored {stored_version}."
                )
            session.version = stored_version + 1
            self._sessions[session.session_id] = session
            self._versions[session.session_id] = session.version

    async def load(self, session_id: str) -> ConversationSession | None:
        async with self._lock:
            return self._sessions.get(session_id)
