"""Laws for the content-addressed blob store — the object room.

1. Round-trip law: what ``put`` accepted, ``get`` returns, byte for byte.
2. Identity law: same content → same digest (idempotence, dedupe); different
   content → different digest.
3. Miss law: an unknown digest is ``None`` — a miss is a normal path.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import TypeAlias

from prodagent.backends.file.blob import FileBlobStore
from prodagent.backends.memory.blob import InMemoryBlobStore
from prodagent.base.blobs import BLOB_REF_KEY, digest_of
from prodagent.ports.persistence import BlobStore

Factory: TypeAlias = Callable[[], BlobStore]


async def _run_round_trip_law(make: Factory) -> None:
    store = make()
    body = "x" * 100_000 + "终点 sentinel"
    digest = await store.put(body)
    assert digest == digest_of(body), "digest is the sha256 of the utf-8 body"
    assert await store.get(digest) == body


async def _run_identity_law(make: Factory) -> None:
    store = make()
    d1 = await store.put("same content")
    d2 = await store.put("same content")
    d3 = await store.put("different content")
    assert d1 == d2, "same content lands on one digest — idempotent by address"
    assert d1 != d3


async def _run_miss_law(make: Factory) -> None:
    store = make()
    assert await store.get("0" * 64) is None


async def test_memory_blob_round_trip():
    await _run_round_trip_law(InMemoryBlobStore)


async def test_memory_blob_identity():
    await _run_identity_law(InMemoryBlobStore)


async def test_memory_blob_miss():
    await _run_miss_law(InMemoryBlobStore)


async def test_file_blob_round_trip(tmp_path):
    await _run_round_trip_law(lambda: FileBlobStore(tmp_path / "blobs"))


async def test_file_blob_identity_and_shard_layout(tmp_path):
    store = FileBlobStore(tmp_path / "blobs")
    d1 = await store.put("same content")
    await store.put("same content")
    d2 = await store.put("different content")
    assert d1 != d2
    # Shard layout: digest ab12… lives under ab/ — one file per body, and the
    # duplicate put wrote no second file.
    files = sorted(p.name for p in (tmp_path / "blobs").rglob("*") if p.is_file())
    assert files == sorted({d1, d2})


async def test_file_blob_miss(tmp_path):
    await _run_miss_law(lambda: FileBlobStore(tmp_path / "blobs"))


async def test_marker_is_reserved_namespace():
    """A plain dict carrying $blob as user data is NOT a ref — fetch_ref
    passes it through untouched (the marker only means something where the
    recorders create it)."""
    from prodagent.base.blobs import fetch_ref

    store = InMemoryBlobStore()
    user_data = {BLOB_REF_KEY: "not-a-digest", "note": "user payload"}
    assert await fetch_ref(user_data, store) == user_data
