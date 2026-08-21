"""Qdrant ``VectorStore`` — HNSW + cosine on a dedicated vector engine."""

from __future__ import annotations

from typing import Any

from prodagent.ports.vector import VectorHit

__all__ = ["QdrantVectorStore"]


class QdrantVectorStore:
    """ANN over Qdrant — one collection per (collection, dim)."""

    def __init__(
        self,
        url: str,
        collection: str,
        dim: int,
        *,
        namespace: str = "default",
        api_key: str | None = None,
    ) -> None:
        from qdrant_client import QdrantClient
        from qdrant_client.http.models import Distance, VectorParams

        self._ns = namespace
        self._collection = collection
        self._client: Any = QdrantClient(url=url, api_key=api_key)
        # Create the collection if missing — idempotent.
        cols = {c.name for c in self._client.get_collections().collections}
        if collection not in cols:
            self._client.create_collection(
                collection_name=collection,
                vectors_config=VectorParams(size=dim, distance=Distance.COSINE),
            )

    async def upsert(
        self, id: str, embedding: list[float], metadata: dict[str, Any] | None = None
    ) -> None:
        from qdrant_client.http.models import PointStruct

        self._client.upsert(
            collection_name=self._collection,
            points=[
                PointStruct(
                    id=_point_id(id),
                    vector=list(embedding),
                    payload={"id": id, "namespace": self._ns, **(metadata or {})},
                )
            ],
        )

    async def search(
        self,
        query: list[float],
        top_k: int = 5,
        filter: dict[str, Any] | None = None,
    ) -> list[VectorHit]:
        from qdrant_client.http.models import FieldCondition, Filter, MatchValue

        conditions: list[Any] = [FieldCondition(key="namespace", match=MatchValue(value=self._ns))]
        if filter:
            for k, v in filter.items():
                conditions.append(FieldCondition(key=k, match=MatchValue(value=v)))
        results = self._client.query_points(
            collection_name=self._collection,
            query=list(query),
            limit=top_k,
            query_filter=Filter(must=conditions),
        ).points
        # Qdrant cosine: higher score = more similar. Score is already similarity.
        return [
            VectorHit(
                id=r.payload.get("id", str(r.id)),
                score=float(r.score),
                metadata={k: v for k, v in r.payload.items() if k not in ("id", "namespace")},
            )
            for r in results
        ]

    async def delete(self, id: str) -> None:
        import contextlib

        from qdrant_client.http.models import PointIdsList

        with contextlib.suppress(Exception):
            self._client.delete(
                collection_name=self._collection,
                points_selector=PointIdsList(points=[_point_id(id)]),
            )


def _point_id(id: str) -> int:
    import hashlib

    h = hashlib.blake2b(id.encode(), digest_size=8).digest()
    return int.from_bytes(h, "big")
