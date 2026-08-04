"""File-based session store — single latest JSON per session_id."""

from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path
from typing import TYPE_CHECKING, Any

from prodagent.backends.file._locking import _exclusive
from prodagent.core.exceptions import VersionConflict
from prodagent.core.io import safe_filename_component, write_atomic_json

if TYPE_CHECKING:
    from prodagent.core.state.session import ConversationSession

logger = logging.getLogger(__name__)

__all__ = ["FileSessionStore"]


class FileSessionStore:
    """Latest-snapshot JSON session store backed by the local filesystem."""

    def __init__(self, directory: str | Path = ".prodagent/sessions", fsync: bool = False) -> None:
        self._dir = Path(directory)
        self._dir.mkdir(parents=True, exist_ok=True)
        self._fsync = fsync

    def _base(self, session_id: str) -> str:
        return safe_filename_component(session_id)

    def _path(self, session_id: str) -> Path:
        return self._dir / f"{self._base(session_id)}.json"

    def _lock_path(self, session_id: str) -> Path:
        return self._dir / f"{self._base(session_id)}.lock"

    async def save(self, session: ConversationSession, expected_version: int | None = None) -> None:
        await asyncio.to_thread(self._save_sync, session, expected_version)

    def _save_sync(self, session: ConversationSession, expected_version: int | None) -> None:
        with _exclusive(self._lock_path(session.session_id)):
            path = self._path(session.session_id)
            stored_version = 0
            if path.exists():
                try:
                    stored_version = int(
                        json.loads(path.read_text(encoding="utf-8")).get("version", 0)
                    )
                except (json.JSONDecodeError, OSError):
                    stored_version = 0

            if expected_version is not None and expected_version != stored_version:
                raise VersionConflict(
                    f"session version mismatch for session={session.session_id}: "
                    f"expected {expected_version}, stored {stored_version}."
                )

            session.version = stored_version + 1
            envelope: dict[str, Any] = {"version": session.version, "session": session.to_dict()}
            write_atomic_json(path, envelope, fsync=self._fsync)

    async def load(self, session_id: str) -> ConversationSession | None:
        return await asyncio.to_thread(self._load_sync, session_id)

    def _load_sync(self, session_id: str) -> ConversationSession | None:
        from prodagent.core.state.session import ConversationSession

        path = self._path(session_id)
        if not path.exists():
            return None
        try:
            envelope = json.loads(path.read_text(encoding="utf-8"))
            session = ConversationSession.from_dict(envelope["session"])
            session.version = int(envelope.get("version", 0))
        except (json.JSONDecodeError, KeyError, TypeError, ValueError) as exc:
            logger.warning("[session] corrupted session file %s: %s", path, exc)
            return None
        return session
