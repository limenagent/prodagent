"""Run the port conformance suite against the ``qdrant`` vector backend.

Qdrant is a dedicated vector database — that is where embeddings belong. The
only port run against it here is ``VectorStore``. Relational, ephemeral, and
graph data have their own engines (see the other ``test_conformance_*``
files).

Requires a running Qdrant — set ``QDRANT_URL`` or default to
``http://localhost:6333``. The whole module is skipped if Qdrant is
unreachable.
"""

from __future__ import annotations

import os
import uuid

import pytest

from prodagent.backends.conformance import (
    run_vector_delete_conformance,
    run_vector_empty_search_conformance,
    run_vector_filter_conformance,
    run_vector_upsert_replaces_conformance,
    run_vector_upsert_search_conformance,
)
from prodagent.backends.qdrant.vector import QdrantVectorStore


def _qdrant_url() -> str:
    return os.getenv("QDRANT_URL", "http://localhost:6333")


def _ping_qdrant() -> bool:
    try:
        import urllib.request

        import qdrant_client

        with urllib.request.urlopen(f"{_qdrant_url()}/healthz", timeout=2) as resp:
            return resp.status == 200 and bool(qdrant_client)
    except Exception:
        return False


pytestmark = pytest.mark.skipif(not _ping_qdrant(), reason="Qdrant not reachable")


@pytest.fixture
def collection() -> str:
    return f"conf-{uuid.uuid4().hex[:8]}"


@pytest.fixture
def ns() -> str:
    return f"test-{uuid.uuid4().hex[:8]}"


@pytest.fixture
def make_store(collection, ns):
    """Factory that builds a store pointing at one shared collection/ns pair.

    Each test gets a fresh, empty namespace: the factory clears the namespace's
    points before handing back the store, so conformance functions see a clean
    slate even though they share a collection.
    """
    created = []

    def _factory() -> QdrantVectorStore:
        store = QdrantVectorStore(
            url=_qdrant_url(),
            collection=collection,
            dim=3,
            namespace=ns,
        )
        # Wipe this namespace's points so each test starts empty.
        from qdrant_client.http.models import FieldCondition, Filter, MatchValue

        store._client.delete(
            collection,
            points_selector=Filter(
                must=[FieldCondition(key="namespace", match=MatchValue(value=ns))]
            ),
        )
        created.append(store)
        return store

    # Build once to ensure the collection exists before tests run.
    _factory()
    yield _factory

    if created:
        import contextlib

        with contextlib.suppress(Exception):
            created[0]._client.delete_collection(collection)


def test_qdrant_vector_upsert_search_conformance(make_store):
    run_vector_upsert_search_conformance(make_store)


def test_qdrant_vector_upsert_replaces_conformance(make_store):
    run_vector_upsert_replaces_conformance(make_store)


def test_qdrant_vector_filter_conformance(make_store):
    run_vector_filter_conformance(make_store)


def test_qdrant_vector_delete_conformance(make_store):
    run_vector_delete_conformance(make_store)


def test_qdrant_vector_empty_search_conformance(make_store):
    run_vector_empty_search_conformance(make_store)
