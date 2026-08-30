"""In-memory blob store — tests and the bare profile's spill-free default."""

from __future__ import annotations

from prodagent.base.blobs import digest_of

__all__ = ["InMemoryBlobStore"]


class InMemoryBlobStore:
    """Digest-keyed dict — same contract as :class:`FileBlobStore`."""

    def __init__(self) -> None:
        self._blobs: dict[str, str] = {}

    async def put(self, text: str) -> str:
        digest = digest_of(text)
        self._blobs.setdefault(digest, text)
        return digest

    async def get(self, digest: str) -> str | None:
        return self._blobs.get(digest)
