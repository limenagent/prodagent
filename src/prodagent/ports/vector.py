"""VectorStore port — ANN search over embedding vectors.

The storage primitive for RAG: write an embedding + metadata, later search by
embedding to get top-k nearest neighbours. Implementations pick the index —
pgvector (HNSW on Postgres), Qdrant, Milvus, or an in-process brute-force for
local dev. The port is silent about the index; callers only see upsert /
search / delete.

Dimension is fixed per store instance — embeddings live in one space. Mixed
dimensions in the same store is a caller bug.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable


@dataclass(frozen=True)
class VectorHit:
    """One neighbour returned by ``search``.

    ``score`` is similarity (higher = closer) for cosine, so callers can sort
    without knowing the distance metric. Distance-based backends invert.
    """

    id: str
    score: float
    metadata: dict[str, Any]


@runtime_checkable
class VectorStore(Protocol):
    """Approximate nearest-neighbour search over embedding vectors."""

    async def upsert(
        self, id: str, embedding: list[float], metadata: dict[str, Any] | None = None
    ) -> None:
        """Insert or replace the vector for ``id``. Metadata is merged on replace."""
        ...

    async def search(
        self,
        query: list[float],
        top_k: int = 5,
        filter: dict[str, Any] | None = None,
    ) -> list[VectorHit]:
        """Return the ``top_k`` nearest vectors to ``query``, most-similar first.

        ``filter`` is an exact-match AND filter over metadata keys — backends
        that support richer filters may extend, but must at least honour
        ``{key: value}`` equality.
        """
        ...

    async def delete(self, id: str) -> None:
        """Remove the vector for ``id``. No-op if missing."""
        ...
