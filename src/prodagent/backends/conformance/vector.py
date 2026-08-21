"""Conformance tests for ``VectorStore`` implementations."""

from __future__ import annotations

from collections.abc import Callable
from typing import TypeAlias

from prodagent.ports.vector import VectorHit, VectorStore

Factory: TypeAlias = Callable[[], VectorStore]


async def run_vector_upsert_search_conformance(make_store: Factory) -> None:
    """search returns the exact-match vector first, others by similarity."""
    store = make_store()
    await store.upsert("a", [1.0, 0.0, 0.0], {"label": "x"})
    await store.upsert("b", [0.0, 1.0, 0.0], {"label": "x"})
    await store.upsert("c", [0.9, 0.1, 0.0], {"label": "y"})

    hits = await store.search([1.0, 0.0, 0.0], top_k=3)
    assert len(hits) == 3
    assert hits[0].id == "a", "exact match must rank first"
    # c is closer to the query than b (both off-axis, c barely so)
    assert hits[1].id == "c"
    assert hits[2].id == "b"
    assert all(isinstance(h, VectorHit) for h in hits)


async def run_vector_upsert_replaces_conformance(make_store: Factory) -> None:
    """Re-upserting the same id replaces the vector, no duplicate rows."""
    store = make_store()
    await store.upsert("k", [1.0, 0.0, 0.0])
    await store.upsert("k", [0.0, 1.0, 0.0], {"tag": "v"})

    hits = await store.search([0.0, 1.0, 0.0], top_k=5)
    assert len(hits) == 1, "upsert must replace, not append"
    assert hits[0].id == "k"
    assert hits[0].metadata == {"tag": "v"}


async def run_vector_filter_conformance(make_store: Factory) -> None:
    """``filter`` narrows to metadata-exact matches."""
    store = make_store()
    await store.upsert("a", [1.0, 0.0, 0.0], {"domain": "work"})
    await store.upsert("b", [0.9, 0.1, 0.0], {"domain": "work"})
    await store.upsert("c", [0.8, 0.2, 0.0], {"domain": "home"})

    hits = await store.search([1.0, 0.0, 0.0], top_k=10, filter={"domain": "work"})
    assert {h.id for h in hits} == {"a", "b"}


async def run_vector_delete_conformance(make_store: Factory) -> None:
    """``delete`` removes the vector; deleting a missing id is a no-op."""
    store = make_store()
    await store.upsert("a", [1.0, 0.0, 0.0])
    await store.upsert("b", [0.0, 1.0, 0.0])

    await store.delete("a")
    hits = await store.search([1.0, 0.0, 0.0], top_k=5)
    assert {h.id for h in hits} == {"b"}

    # No-op on missing — must not raise.
    await store.delete("nope")
    assert len(await store.search([0.0, 1.0, 0.0], top_k=5)) == 1


async def run_vector_empty_search_conformance(make_store: Factory) -> None:
    """A fresh store returns no hits and does not raise."""
    store = make_store()
    assert await store.search([1.0, 0.0, 0.0], top_k=5) == []
