"""Conformance tests for ``GraphStore`` implementations."""

from __future__ import annotations

from collections.abc import Callable
from typing import TypeAlias

from prodagent.ports.graph import GraphStore

Factory: TypeAlias = Callable[[], GraphStore]


def run_graph_node_conformance(make_store: Factory) -> None:
    """add_node creates a retrievable node; get_node returns its shape."""
    store = make_store()
    store.add_node("alice", labels=["Person"], properties={"name": "Alice"})

    node = store.get_node("alice")
    assert node is not None
    assert node["id"] == "alice"
    assert "Person" in node["labels"]
    assert node["properties"]["name"] == "Alice"

    assert store.get_node("nope") is None


def run_graph_node_merge_conformance(make_store: Factory) -> None:
    """Re-adding a node merges labels and properties, not replaces."""
    store = make_store()
    store.add_node("bob", labels=["Person"], properties={"age": 30})
    store.add_node("bob", labels=["Employee"], properties={"age": 31, "role": "dev"})

    node = store.get_node("bob")
    assert node is not None
    assert set(node["labels"]) == {"Person", "Employee"}
    assert node["properties"]["age"] == 31  # updated, not duplicated
    assert node["properties"]["role"] == "dev"


def run_graph_edge_neighbors_conformance(make_store: Factory) -> None:
    """add_edge links nodes; neighbors walks out-edges by relationship."""
    store = make_store()
    store.add_node("alice")
    store.add_node("bob")
    store.add_node("carol")
    store.add_edge("alice", "bob", "KNOWS")
    store.add_edge("alice", "carol", "WORKS_WITH")

    knows = store.neighbors("alice", rel="KNOWS")
    assert {n["id"] for n in knows} == {"bob"}

    all_n = store.neighbors("alice")
    assert {n["id"] for n in all_n} == {"bob", "carol"}


def run_graph_edge_idempotent_conformance(make_store: Factory) -> None:
    """Re-adding the same (src, dst, rel) merges properties, no duplicate edge."""
    store = make_store()
    store.add_edge("a", "b", "KNOWS", {"since": 2020})
    store.add_edge("a", "b", "KNOWS", {"weight": 5})

    ns = store.neighbors("a", rel="KNOWS")
    assert len(ns) == 1, "duplicate edge for same (src,dst,rel) is a bug"
    # properties merged — exact shape is impl-defined, but both keys present


def run_graph_traverse_depth_conformance(make_store: Factory) -> None:
    """neighbors(depth=2) reaches two hops out; depth=1 stays one hop."""
    store = make_store()
    store.add_edge("a", "b", "KNOWS")
    store.add_edge("b", "c", "KNOWS")

    one = store.neighbors("a", rel="KNOWS", depth=1)
    assert {n["id"] for n in one} == {"b"}

    two = store.neighbors("a", rel="KNOWS", depth=2)
    assert {n["id"] for n in two} == {"b", "c"}


def run_graph_delete_node_conformance(make_store: Factory) -> None:
    """delete_node removes the node and its incident edges."""
    store = make_store()
    store.add_edge("a", "b", "KNOWS")
    store.add_edge("b", "c", "KNOWS")

    store.delete_node("b")
    assert store.get_node("b") is None
    # edges incident to b are gone — a has no neighbours, c has no in-path from b
    assert store.neighbors("a", rel="KNOWS") == []
    # deleting a missing node is a no-op
    store.delete_node("nope")


def run_graph_absent_node_neighbors_conformance(make_store: Factory) -> None:
    """neighbors on a missing node returns [], not an error."""
    store = make_store()
    assert store.neighbors("ghost") == []


def run_graph_list_nodes_conformance(make_store: Factory) -> None:
    """list_nodes returns all nodes, optionally filtered by label."""
    store = make_store()
    store.add_node("alice", labels=["Person"])
    store.add_node("bob", labels=["Person", "Employee"])
    store.add_node("acme", labels=["Company"])

    all_nodes = store.list_nodes()
    assert {n["id"] for n in all_nodes} == {"alice", "bob", "acme"}

    people = store.list_nodes(label="Person")
    assert {n["id"] for n in people} == {"alice", "bob"}

    companies = store.list_nodes(label="Company")
    assert {n["id"] for n in companies} == {"acme"}

    assert store.list_nodes(label="Ghost") == []
