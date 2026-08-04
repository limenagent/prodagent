"""In-process ``VectorStore`` — brute-force cosine similarity."""

from __future__ import annotations

from typing import Any

from prodagent.cognition.memory.embedder import cosine as _cosine
from prodagent.ports.vector import VectorHit

__all__ = ["InMemoryVectorStore"]


class InMemoryVectorStore:
    """Brute-force cosine ANN — O(n) per search, zero dependencies."""

    def __init__(self) -> None:
        self._vecs: dict[str, list[float]] = {}
        self._meta: dict[str, dict[str, Any]] = {}

    def upsert(
        self, id: str, embedding: list[float], metadata: dict[str, Any] | None = None
    ) -> None:
        self._vecs[id] = list(embedding)
        if metadata is None:
            self._meta.pop(id, None)
        else:
            self._meta[id] = dict(metadata)

    def search(
        self,
        query: list[float],
        top_k: int = 5,
        filter: dict[str, Any] | None = None,
    ) -> list[VectorHit]:
        hits: list[VectorHit] = []
        for vid, vec in self._vecs.items():
            if filter is not None:
                meta = self._meta.get(vid, {})
                if any(meta.get(k) != v for k, v in filter.items()):
                    continue
            hits.append(
                VectorHit(id=vid, score=_cosine(query, vec), metadata=self._meta.get(vid, {}))
            )
        hits.sort(key=lambda h: h.score, reverse=True)
        return hits[:top_k]

    def delete(self, id: str) -> None:
        self._vecs.pop(id, None)
        self._meta.pop(id, None)
