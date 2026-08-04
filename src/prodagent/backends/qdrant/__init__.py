"""Qdrant-backed ``VectorStore`` — dedicated vector database for ANN search.

Qdrant is a purpose-built vector search engine: HNSW index, payload filters,
cosine/dot/Euclidean distances. This is where embeddings belong — not stuffed
into a relational DB via pgvector, not hashed into a KV. Vectors are a
first-class data type with their own storage engine.

Requires the ``[qdrant]`` extra::

    pip install prodagent[qdrant]

One collection per ``(collection_name, dim)`` — created on init if missing.
Namespace isolation is via Qdrant payload field ``namespace``, filtered in
every search so multiple agents sharing one collection do not collide.
"""

from __future__ import annotations

from prodagent.backends.qdrant.vector import QdrantVectorStore

__all__ = ["QdrantVectorStore"]
