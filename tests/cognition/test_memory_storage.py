from __future__ import annotations

import json

from prodagent.backends.file import FileDocumentStore
from prodagent.backends.memory import InMemoryGraphStore
from prodagent.cognition.memory.storage import (
    MemoryRecord,
    MemoryType,
)
from prodagent.ports.document import DocumentStore
from prodagent.ports.graph import GraphStore


def test_protocols_are_runtime_checkable(tmp_path):
    doc = FileDocumentStore(tmp_path)
    graph = InMemoryGraphStore()
    assert isinstance(doc, DocumentStore)
    assert isinstance(graph, GraphStore)


async def test_document_store_constraints_round_trip(tmp_path):
    store = FileDocumentStore(tmp_path)
    assert await store.load_constraints() == []
    await store.append_soft(MemoryRecord(content="禁止ORM", memory_type=MemoryType.CONSTRAINT))
    await store.append_soft(MemoryRecord(content="必须审批", memory_type=MemoryType.CONSTRAINT))
    constraints = await store.load_constraints()
    assert {c.content for c in constraints} == {"禁止ORM", "必须审批"}
    assert all(c.memory_type is MemoryType.CONSTRAINT for c in constraints)


async def test_document_store_append_soft_dedups_by_content(tmp_path):
    store = FileDocumentStore(tmp_path)
    await store.append_soft(MemoryRecord(content="rule A", memory_type=MemoryType.CONSTRAINT))
    await store.append_soft(MemoryRecord(content="rule A", memory_type=MemoryType.CONSTRAINT))
    await store.append_soft(MemoryRecord(content="rule B", memory_type=MemoryType.CONSTRAINT))
    constraints = await store.load_constraints()
    assert len(constraints) == 3


async def test_document_store_memories_only_soft(tmp_path):
    store = FileDocumentStore(tmp_path)
    soft = [
        {"content": "user likes Python", "memory_type": "preference"},
        {"content": "yesterday's outage", "memory_type": "episodic"},
    ]
    store._memories_file.write_text(json.dumps(soft))

    docs = await store.load_memories()
    assert len(docs) == 2
    assert all(m.memory_type != "fact" for m in docs)


async def test_graph_store_node_round_trip():
    """add_node stores labels + properties; get_node returns them."""
    graph = InMemoryGraphStore()
    await graph.add_node("pod:payment", labels=["Fact"], properties={"content": "v2.14"})

    node = await graph.get_node("pod:payment")
    assert node is not None
    assert "Fact" in node["labels"]
    assert node["properties"]["content"] == "v2.14"


async def test_graph_store_list_nodes_by_label():
    """list_nodes(label='Fact') returns only fact nodes."""
    graph = InMemoryGraphStore()
    await graph.add_node("pod:x", labels=["Fact"])
    await graph.add_node("pod:y", labels=["Fact"])
    await graph.add_node("svc:a", labels=["Service"])

    facts = await graph.list_nodes(label="Fact")
    assert {n["id"] for n in facts} == {"pod:x", "pod:y"}


async def test_graph_store_node_merge_bumps_version():
    """Re-adding the same node merges properties — caller bumps version."""
    graph = InMemoryGraphStore()
    await graph.add_node(
        "pod:payment", labels=["Fact"], properties={"version": 1, "content": "v2.14"}
    )
    await graph.add_node(
        "pod:payment", labels=["Fact"], properties={"version": 2, "content": "v2.15"}
    )

    node = await graph.get_node("pod:payment")
    assert node["properties"]["version"] == 2
    assert node["properties"]["content"] == "v2.15"


async def test_document_store_append_soft_episodic_gets_default_ttl(tmp_path):
    store = FileDocumentStore(tmp_path)
    record = MemoryRecord(content="an outage happened yesterday", memory_type=MemoryType.EPISODIC)
    await store.append_soft(record)

    mem = (await store.load_memories())[0]
    assert mem.ttl_days == 7


async def test_document_store_append_soft_preference_has_no_ttl(tmp_path):
    store = FileDocumentStore(tmp_path)
    record = MemoryRecord(content="user prefers concise answers", memory_type=MemoryType.PREFERENCE)
    await store.append_soft(record)

    mem = (await store.load_memories())[0]
    assert mem.ttl_days is None


async def test_fact_and_soft_memories_are_isolated(tmp_path):
    """Facts live in the graph store; soft memories in the document store.

    The two stores never share state — a fact node is not visible as a soft
    memory, and vice versa.
    """
    doc = FileDocumentStore(tmp_path)
    graph = InMemoryGraphStore()

    await doc.append_soft(
        MemoryRecord(content="user likes Python", memory_type=MemoryType.PREFERENCE)
    )
    await graph.add_node("pod:x", labels=["Fact"], properties={"content": "pod v2.14"})

    # document store has only the soft memory
    soft = await doc.load_memories()
    assert {m.memory_type for m in soft} == {"preference"}
    # graph store has only the fact node
    facts = await graph.list_nodes(label="Fact")
    assert {n["id"] for n in facts} == {"pod:x"}
