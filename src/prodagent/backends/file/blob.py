"""File-backed blob store — a sharded directory is the object library.

Content addressing does the layout: digest ``ab12…`` lives at
``blobs/ab/ab12…`` (a two-hex-char shard keeps any one directory small
when the store grows). Writes are atomic (tmp + rename) and idempotent —
an existing body means the content is already there, which is the whole
point of addressing by content.
"""

from __future__ import annotations

import asyncio
import os
import tempfile
from pathlib import Path

from prodagent.base.blobs import digest_of

__all__ = ["FileBlobStore"]


class FileBlobStore:
    """Content-addressed bodies under one directory, shard-per-prefix."""

    def __init__(self, directory: str | Path) -> None:
        self._dir = Path(directory)
        self._dir.mkdir(parents=True, exist_ok=True)

    def _path(self, digest: str) -> Path:
        return self._dir / digest[:2] / digest

    def _put_sync(self, text: str) -> str:
        digest = digest_of(text)
        path = self._path(digest)
        if path.exists():
            return digest  # content-addressed idempotence: it is already there
        path.parent.mkdir(parents=True, exist_ok=True)
        # tmp + rename: a crash mid-write can never leave a half body under
        # the digest (a torn body would poison every pointer to it).
        fd, tmp = tempfile.mkstemp(dir=path.parent, suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(text)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, path)
        finally:
            if os.path.exists(tmp):
                os.unlink(tmp)
        return digest

    async def put(self, text: str) -> str:
        return await asyncio.to_thread(self._put_sync, text)

    async def get(self, digest: str) -> str | None:
        def _read() -> str | None:
            path = self._path(digest)
            if not path.exists():
                return None
            return path.read_text(encoding="utf-8")

        return await asyncio.to_thread(_read)
