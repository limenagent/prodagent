from __future__ import annotations

import pytest

from prodagent.backends.file import FileDocumentStore
from prodagent.backends.memory import InMemoryGraphStore
from prodagent.cognition.context.budget import TokenCounter
from prodagent.cognition.memory.channels import RecallContext, SemanticChannel
from prodagent.cognition.memory.manager import MemoryManager
from prodagent.cognition.memory.storage import (
    MemoryRecord,
    MemoryType,
    StoredMemory,
)


class _SpyEmbedder:
    def __init__(self) -> None:
        self.calls: list[str] = []

    def embed(self, text: str) -> list[float]:
        self.calls.append(text)
        vec = [0.0] * 8
        for ch in text:
            vec[ord(ch) % 8] += 1.0
        norm = sum(v * v for v in vec) ** 0.5
        if norm > 0:
            vec = [v / norm for v in vec]
        return vec


@pytest.fixture
def stores(tmp_path):
    return FileDocumentStore(tmp_path), InMemoryGraphStore()


@pytest.fixture
def manager(stores):
    docs, facts = stores
    embedder = _SpyEmbedder()
    return MemoryManager(docs, facts, embedder=embedder), embedder


class TestEmbeddingCache:
    async def test_write_memory_caches_embedding_on_record(self, manager):
        mgr, embedder = manager
        record = MemoryRecord(
            content="user prefers dark mode",
            memory_type=MemoryType.PREFERENCE,
        )
        await mgr._write_memory(record)
        mems = await mgr._documents.load_memories()
        assert len(mems) == 1
        assert mems[0].embedding is not None
        assert len(mems[0].embedding) > 0

    async def test_add_memory_caches_embedding(self, manager):
        mgr, embedder = manager
        record = MemoryRecord(
            content="pod-x is at v3",
            memory_type=MemoryType.FACT,
            entity_id="pod-x",
        )
        await mgr.add_memory(record)
        facts = await mgr._facts.load_all()
        assert len(facts) == 1
        assert facts[0].embedding is not None

    async def test_embedding_survives_round_trip(self, stores):
        docs, _ = stores
        mem = StoredMemory(
            id="m1",
            content="hello",
            memory_type=MemoryType.PREFERENCE,
            embedding=[0.1, 0.2, 0.3],
        )
        await docs.save_memories([mem])
        loaded = await docs.load_memories()
        assert len(loaded) == 1
        assert loaded[0].embedding == [0.1, 0.2, 0.3]

    def test_recall_uses_cached_embedding_no_reembed(self, manager):
        mgr, embedder = manager
        record = MemoryRecord(
            content="user prefers dark mode",
            memory_type=MemoryType.PREFERENCE,
            domain="ui",
        )
        import asyncio

        asyncio.run(mgr._write_memory(record))
        embedder.calls.clear()

        asyncio.run(mgr.recall("dark mode preference", domain="ui"))
        query_embeds = [c for c in embedder.calls if c == "dark mode preference"]
        assert len(query_embeds) == 1
        assert "user prefers dark mode" not in embedder.calls

    async def test_conflict_filter_uses_cached_embedding(self, manager):
        mgr, embedder = manager

        await mgr._write_memory(
            MemoryRecord(
                content="user prefers dark mode",
                memory_type=MemoryType.PREFERENCE,
                domain="ui",
            )
        )
        embedder.calls.clear()

        new_mem = StoredMemory(
            id="new1",
            content="user prefers dark theme",
            memory_type=MemoryType.PREFERENCE,
            domain="ui",
            embedding=embedder.embed("user prefers dark theme"),
        )
        embedder.calls.clear()
        await mgr._conflict_pipeline._filter.candidates(new_mem, mgr._documents)
        assert "user prefers dark mode" not in embedder.calls

    async def test_fallback_to_embedder_when_embedding_none(self, stores):
        docs, _facts = stores
        embedder = _SpyEmbedder()
        legacy_mem = StoredMemory(
            id="legacy1",
            content="legacy memory",
            memory_type=MemoryType.PREFERENCE,
            domain="ui",
            embedding=None,
        )
        await docs.save_memories([legacy_mem])

        channel = SemanticChannel(embedder)
        ctx = RecallContext(
            constraints=[],
            documents=await docs.load_memories(),
            facts=[],
            static_constraints=[],
            counter=TokenCounter(),
        )
        await channel.search("legacy", "ui", ctx=ctx)
        assert "legacy memory" in embedder.calls
