"""Conformance tests for ``DocumentStore`` implementations.

DocumentStore methods are synchronous on the port.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import TypeAlias

from prodagent.ports.document import DocumentStore, MemoryRecord, MemoryType

Factory: TypeAlias = Callable[[], DocumentStore]


async def run_document_conformance(make_store: Factory) -> None:
    store = make_store()

    assert await store.load_memories() == [], "fresh store has no memories"
    assert await store.load_constraints() == [], "fresh store has no constraints"

    await store.append_soft(
        MemoryRecord(content="user likes dark mode", memory_type=MemoryType.PREFERENCE)
    )
    mems = await store.load_memories()
    assert len(mems) == 1
    assert mems[0].content == "user likes dark mode"
    assert mems[0].memory_type == MemoryType.PREFERENCE


async def run_document_supersede_conformance(make_store: Factory) -> None:
    store = make_store()
    await store.append_soft(MemoryRecord(content="v1", memory_type=MemoryType.PREFERENCE))
    mem_id = (await store.load_memories())[0].id

    await store.mark_superseded(mem_id, True)
    loaded = next(m for m in await store.load_memories() if m.id == mem_id)
    assert loaded.superseded is True

    await store.mark_superseded(mem_id, False)
    loaded = next(m for m in await store.load_memories() if m.id == mem_id)
    assert loaded.superseded is False


async def run_document_touch_conformance(make_store: Factory) -> None:
    store = make_store()
    await store.append_soft(MemoryRecord(content="rec", memory_type=MemoryType.EPISODIC))
    mem_id = (await store.load_memories())[0].id

    before = (await store.load_memories())[0]
    await store.touch_memory(mem_id)
    after = (await store.load_memories())[0]
    assert after.access_count >= before.access_count, "touch must not decrease access_count"


async def run_document_constraint_storage_conformance(make_store: Factory) -> None:
    store = make_store()
    await store.append_soft(
        MemoryRecord(content="never delete prod db", memory_type=MemoryType.CONSTRAINT)
    )
    constraints = await store.load_constraints()
    assert len(constraints) == 1
    assert constraints[0].memory_type == MemoryType.CONSTRAINT
