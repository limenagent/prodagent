"""blob — storage for large bytes; artifacts point into it.

The event stream must stay small and uniform. When an agent produces a file (a
report, a spreadsheet, an image), the *bytes* go to a BlobStore and only a
*pointer* (filename/version/uri/mime/size) is recorded as an ARTIFACT_WRITTEN
event. Artifacts are therefore, just like state and trace, a projection of the
event stream:

    replay the pointer events  -> the artifact library (what files, which versions)
    call BlobStore.load(uri)   -> the bytes themselves

Three pieces:
- BlobStore: the port — save assigns the next version and uri, load fetches;
- InMemoryBlobStore: the zero-side-effect default (same spirit as the in-memory
  event log); a durable local-directory implementation lives in backends;
- artifacts_from_events / latest_artifacts: pure projections over the stream.

Versioning is the store's job (it is what actually holds the bytes): each save
of the same filename in a run gets the next number, derived as one past the
highest version *still present* — never a count, so deleting an intermediate
version can't make the next save overwrite a file that is there. The in-memory
and local stores share that one rule (next_version), so the two backends cannot
drift apart. The filename is reduced to its basename, so a name like ``../x``
can never address outside the run directory.
"""

from __future__ import annotations

import json
import os
from collections.abc import Iterable
from typing import Any, Protocol

from src.kernel.eventlog import ARTIFACT_WRITTEN

# Minimal extension -> MIME table (the teaching build infers the common cases
# without a third-party mimetypes dependency).
_EXT_MIME = {
    ".md": "text/markdown",
    ".txt": "text/plain",
    ".json": "application/json",
    ".csv": "text/csv",
    ".html": "text/html",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".pdf": "application/pdf",
}


def _guess_mime(filename: str, data: Any) -> str:
    name = filename.lower()
    for ext, mime in _EXT_MIME.items():
        if name.endswith(ext):
            return mime
    return "text/plain" if isinstance(data, str) else "application/octet-stream"


def _as_bytes(data: Any) -> bytes:
    if isinstance(data, str):
        return data.encode("utf-8")
    if isinstance(data, bytes):
        return data
    # Structured values are serialized as JSON text rather than pickled, so an
    # artifact is portable and readable outside the process.
    return json.dumps(data, ensure_ascii=False, indent=2, default=str).encode("utf-8")


def next_version(used: Iterable[int]) -> int:
    """One past the highest version still present (max, never a count).

    With versions v1, v2, v3 on hand the next is v4; if v2 is deleted (v1, v3
    remain) the next is still v4, so it can never silently overwrite the v3 that
    is there; once every version is gone it returns to v1. The in-memory and
    local stores call this one function, so their numbering can never diverge.
    """
    return max(used, default=0) + 1


def _versions_present(uris: Iterable[str], prefix: str) -> list[int]:
    """The version numbers of existing uris that begin with ``prefix``
    (``.../{filename}.v``) — the facts next_version folds."""
    return [int(u[len(prefix) :]) for u in uris if u.startswith(prefix)]


class BlobStore(Protocol):
    async def save(
        self, run_id: str, filename: str, data: Any, mime: str = ""
    ) -> dict[str, Any]: ...
    async def load(self, uri: str) -> bytes: ...
    async def delete(self, uri: str) -> None: ...


class InMemoryBlobStore:
    """Holds bytes in a dict; the next version is folded from the uris present."""

    def __init__(self) -> None:
        self._blobs: dict[str, bytes] = {}

    async def save(self, run_id: str, filename: str, data: Any, mime: str = "") -> dict[str, Any]:
        filename = os.path.basename(filename)  # never address outside the run
        body = _as_bytes(data)
        mime = mime or _guess_mime(filename, data)
        prefix = f"{run_id}/{filename}.v"
        version = next_version(_versions_present(self._blobs, prefix))
        uri = f"{prefix}{version}"
        self._blobs[uri] = body
        return {
            "filename": filename,
            "version": version,
            "uri": uri,
            "mime": mime,
            "size": len(body),
        }

    async def load(self, uri: str) -> bytes:
        return self._blobs[uri]

    async def delete(self, uri: str) -> None:
        self._blobs.pop(uri, None)


# — projections: the artifact library is a fold of pointer events —
def artifacts_from_events(events: Iterable[Any]) -> dict[str, list[dict[str, Any]]]:
    """filename -> every version pointer, in the order they were saved."""
    out: dict[str, list[dict[str, Any]]] = {}
    for ev in events:
        if ev.kind == ARTIFACT_WRITTEN:
            pointer = dict(ev.data)
            out.setdefault(pointer["filename"], []).append(pointer)
    return out


def latest_artifacts(events: Iterable[Any]) -> dict[str, dict[str, Any]]:
    """filename -> its newest version pointer (what a "Files" panel shows)."""
    return {name: versions[-1] for name, versions in artifacts_from_events(events).items()}
