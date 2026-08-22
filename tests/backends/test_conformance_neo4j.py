"""Run the port conformance suite against the ``neo4j`` graph backend.

Neo4j is a dedicated graph database — that is where nodes, edges, and
traversal belong. The only port run against it here is ``GraphStore``.
Relational, ephemeral, and vector data have their own engines (see the
other ``test_conformance_*`` files).

Requires a running Neo4j — set ``NEO4J_URI``/``NEO4J_USER``/``NEO4J_PASSWORD``
or default to ``bolt://localhost:7687`` with ``neo4j``/``password``. The whole
module is skipped if Neo4j is unreachable.

Each test runs in its own isolated namespace (a property stamped on every
node) so concurrent test runs on the same DB do not collide. The namespace's
nodes are deleted before each test.
"""

from __future__ import annotations

import os
import uuid

import pytest

from tests.backends.conformance import (
    run_graph_absent_node_neighbors_conformance,
    run_graph_delete_node_conformance,
    run_graph_edge_idempotent_conformance,
    run_graph_edge_neighbors_conformance,
    run_graph_list_nodes_conformance,
    run_graph_node_conformance,
    run_graph_node_merge_conformance,
    run_graph_traverse_depth_conformance,
)


def _neo4j_uri() -> str:
    return os.getenv("NEO4J_URI", "bolt://localhost:7687")


def _neo4j_user() -> str:
    return os.getenv("NEO4J_USER", "neo4j")


def _neo4j_password() -> str:
    return os.getenv("NEO4J_PASSWORD", "password")


def _ping_neo4j() -> bool:
    try:
        from neo4j import GraphDatabase

        driver = GraphDatabase.driver(_neo4j_uri(), auth=(_neo4j_user(), _neo4j_password()))
        try:
            driver.verify_connectivity()
            return True
        finally:
            driver.close()
    except Exception:
        return False


pytestmark = pytest.mark.skipif(not _ping_neo4j(), reason="Neo4j not reachable")


@pytest.fixture
def ns() -> str:
    return f"test-{uuid.uuid4().hex[:8]}"


class _NamespacedNeo4jStore:
    """Wraps ``Neo4jGraphStore`` so every node carries a ``__ns`` property,
    and ``list_nodes``/``get_node``/``neighbors`` only see this namespace's
    nodes. Gives test isolation without one DB per test."""

    def __init__(self, ns: str) -> None:
        from prodagent.backends.neo4j.graph import Neo4jGraphStore

        self._ns = ns
        self._inner = Neo4jGraphStore(_neo4j_uri(), _neo4j_user(), _neo4j_password())
        self._clean()

    def _clean(self) -> None:
        with self._inner._driver.session() as sess:
            sess.run(
                "MATCH (n:Entity) WHERE n.__ns = $ns DETACH DELETE n",
                ns=self._ns,
            )

    def add_node(self, node_id, labels=None, properties=None):
        # stamp __ns on every node so we can isolate
        props = {"__ns": self._ns, **(properties or {})}
        self._inner.add_node(node_id, labels=labels, properties=props)

    def add_edge(self, src, dst, rel, properties=None):
        # Ensure endpoints carry __ns so namespace filtering sees them.
        with self._inner._driver.session() as sess:
            sess.run(
                "MERGE (a:Entity {id: $src}) MERGE (b:Entity {id: $dst}) "
                "SET a.__ns = $ns, b.__ns = $ns "
                f"MERGE (a)-[r:`{rel.replace('`', '``')}`]->(b) "
                "SET r += $props",
                src=src,
                dst=dst,
                ns=self._ns,
                props=properties or {},
            )

    def get_node(self, node_id):
        node = self._inner.get_node(node_id)
        if node is None or node["properties"].get("__ns") != self._ns:
            return None
        node["properties"].pop("__ns", None)
        return node

    def list_nodes(self, label=None):
        # filter by __ns at the Cypher level
        with self._inner._driver.session() as sess:
            if label is None:
                recs = sess.run(
                    "MATCH (n:Entity) WHERE n.__ns = $ns "
                    "RETURN n.id AS id, labels(n) AS labels, properties(n) AS props",
                    ns=self._ns,
                )
            else:
                lab = label.replace("`", "``")
                recs = sess.run(
                    f"MATCH (n:Entity:`{lab}`) WHERE n.__ns = $ns "
                    "RETURN n.id AS id, labels(n) AS labels, properties(n) AS props",
                    ns=self._ns,
                )
            out = []
            for rec in recs:
                labels = [lab for lab in rec["labels"] if lab != "Entity"]
                props = {k: v for k, v in rec["props"].items() if k not in ("id", "__ns")}
                out.append({"id": rec["id"], "labels": labels, "properties": props})
            return out

    def neighbors(self, node_id, rel=None, depth=1):
        # only return neighbours in this namespace
        all_n = self._inner.neighbors(node_id, rel=rel, depth=depth)
        out = []
        for n in all_n:
            ns = n["properties"].get("__ns")
            if ns == self._ns:
                n["properties"].pop("__ns", None)
                out.append(n)
        return out

    def traverse(self, start, query, params=None):
        return self._inner.traverse(start, query, params)

    def delete_node(self, node_id):
        self._inner.delete_node(node_id)


@pytest.fixture
def make_store(ns):
    store = _NamespacedNeo4jStore(ns)
    return lambda: store


def test_neo4j_graph_node_conformance(make_store):
    run_graph_node_conformance(make_store)


def test_neo4j_graph_node_merge_conformance(make_store):
    run_graph_node_merge_conformance(make_store)


def test_neo4j_graph_edge_neighbors_conformance(make_store):
    run_graph_edge_neighbors_conformance(make_store)


def test_neo4j_graph_edge_idempotent_conformance(make_store):
    run_graph_edge_idempotent_conformance(make_store)


def test_neo4j_graph_traverse_depth_conformance(make_store):
    run_graph_traverse_depth_conformance(make_store)


def test_neo4j_graph_delete_node_conformance(make_store):
    run_graph_delete_node_conformance(make_store)


def test_neo4j_graph_absent_node_neighbors_conformance(make_store):
    run_graph_absent_node_neighbors_conformance(make_store)


def test_neo4j_graph_list_nodes_conformance(make_store):
    run_graph_list_nodes_conformance(make_store)
