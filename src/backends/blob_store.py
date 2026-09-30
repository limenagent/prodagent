"""blob_store — durable, local-directory BlobStore for produced artifacts.

The kernel ships an in-memory BlobStore by default (zero side effect). This is
the file-backed version for real runs: artifact bytes live under one directory,
keyed by run, and writes use "temp file + os.replace" so a crash never leaves a
half-written file. The next version is folded (via the kernel's next_version)
from the highest version already on disk, so a process restart keeps numbering
continuous and deleting an intermediate version can never make a later save
overwrite a file that is present.

It satisfies the kernel's BlobStore protocol; the artifact pointer events and
all projections stay unchanged — only where the bytes live differs.
"""

from __future__ import annotations

import os
import pathlib

from src.kernel.blob import _as_bytes, _guess_mime, next_version


class LocalBlobStore:
    def __init__(self, directory: str):
        self.root = pathlib.Path(directory)
        self.root.mkdir(parents=True, exist_ok=True)

    def _path(self, uri: str) -> pathlib.Path:
        # Clamp to the store root: a caller-supplied uri (an absolute path, or
        # one carrying "..") can never address outside it — load and delete
        # take uris straight off the wire in the playground, so this is a
        # security boundary, not tidiness.
        pure = pathlib.PurePosixPath(uri)
        if pure.is_absolute() or ".." in pure.parts:
            raise ValueError(f"invalid artifact uri: {uri!r}")
        return self.root.joinpath(*pure.parts)

    async def save(self, run_id: str, filename: str, data, mime: str = "") -> dict:
        filename = os.path.basename(filename)  # confine writes to the run directory
        body = _as_bytes(data)
        mime = mime or _guess_mime(filename, data)
        run_dir = self.root / run_id
        run_dir.mkdir(parents=True, exist_ok=True)
        # Fold the next version from the numbered files already on disk (a
        # leftover .tmp is ignored); max, not a count.
        prefix = f"{filename}.v"
        tails = (p.name[len(prefix) :] for p in run_dir.iterdir() if p.name.startswith(prefix))
        version = next_version(int(t) for t in tails if t.isdigit())
        uri = f"{run_id}/{filename}.v{version}"
        path = self._path(uri)
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_bytes(body)
        os.replace(tmp, path)  # same-directory rename is atomic on POSIX and NT
        return {
            "filename": filename,
            "version": version,
            "uri": uri,
            "mime": mime,
            "size": len(body),
        }

    async def load(self, uri: str) -> bytes:
        return self._path(uri).read_bytes()

    async def delete(self, uri: str) -> None:
        self._path(uri).unlink(missing_ok=True)
