"""Conformance tests for ``DocumentStore`` implementations.

DocumentStore methods are synchronous on the port.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import TypeAlias

from prodagent.cognition.memory.storage import MemoryRecord, MemoryType
from prodagent.ports.document import DocumentStore

Factory: TypeAlias = Callable[[], DocumentStore]


def run_document_conformance(make_store: Factory) -> None:
    store = make_store()

    assert store.load_memories() == [], "fresh store has no memories"
    assert store.load_constraints() == [], "fresh store has no constraints"

    store.append_soft(
        MemoryRecord(content="user likes dark mode", memory_type=MemoryType.PREFERENCE)
    )
    mems = store.load_memories()
    assert len(mems) == 1
    assert mems[0].content == "user likes dark mode"
    assert mems[0].memory_type == MemoryType.PREFERENCE


def run_document_supersede_conformance(make_store: Factory) -> None:
    store = make_store()
    store.append_soft(MemoryRecord(content="v1", memory_type=MemoryType.PREFERENCE))
    mem_id = store.load_memories()[0].id

    store.mark_superseded(mem_id, True)
    loaded = next(m for m in store.load_memories() if m.id == mem_id)
    assert loaded.superseded is True

    store.mark_superseded(mem_id, False)
    loaded = next(m for m in store.load_memories() if m.id == mem_id)
    assert loaded.superseded is False


def run_document_touch_conformance(make_store: Factory) -> None:
    store = make_store()
    store.append_soft(MemoryRecord(content="rec", memory_type=MemoryType.EPISODIC))
    mem_id = store.load_memories()[0].id

    before = store.load_memories()[0]
    store.touch_memory(mem_id)
    after = store.load_memories()[0]
    assert after.access_count >= before.access_count, "touch must not decrease access_count"


def run_document_constraint_storage_conformance(make_store: Factory) -> None:
    store = make_store()
    store.append_soft(
        MemoryRecord(content="never delete prod db", memory_type=MemoryType.CONSTRAINT)
    )
    constraints = store.load_constraints()
    assert len(constraints) == 1
    assert constraints[0].memory_type == MemoryType.CONSTRAINT
