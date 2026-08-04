"""GraphStore port — a real graph database for nodes, edges, and traversal.

The previous GraphStore was a JSON-blob KV with ``upsert_fact``/``load_facts``
— no edges, no traversal, no graph structure at all. It was named "graph" but
was not one. This port is the correction: nodes have labels and properties,
edges have a relationship type and properties, and callers can walk the graph
by relationship and depth.

Implementations belong in a graph database (Neo4j, Memgraph, …). An in-memory
adjacency-list implementation exists for local dev and tests — it is not for
production. Relational/KV backends (Postgres, Redis, file) do NOT implement
this port: stuffing a graph into a KV is how we got the broken first version.
"""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable


@runtime_checkable
class GraphStore(Protocol):
    """A directed property graph: nodes + typed edges + neighbour traversal."""

    def add_node(
        self,
        node_id: str,
        labels: list[str] | None = None,
        properties: dict[str, Any] | None = None,
    ) -> None:
        """Insert or merge a node. Re-adding merges labels and properties."""
        ...

    def add_edge(
        self,
        src: str,
        dst: str,
        rel: str,
        properties: dict[str, Any] | None = None,
    ) -> None:
        """Insert a directed edge ``src -[rel]-> dst``. Idempotent on (src,dst,rel)."""
        ...

    def get_node(self, node_id: str) -> dict[str, Any] | None:
        """Return ``{id, labels, properties}`` or ``None`` if absent."""
        ...

    def list_nodes(self, label: str | None = None) -> list[dict[str, Any]]:
        """All nodes, optionally filtered to those carrying ``label``.

        Each entry is ``{id, labels, properties}``. This is a full scan —
        recall uses it to surface every fact node; prefer ``neighbors`` when
        you have a starting point.
        """
        ...

    def neighbors(
        self,
        node_id: str,
        rel: str | None = None,
        depth: int = 1,
    ) -> list[dict[str, Any]]:
        """Out-neighbours of ``node_id`` within ``depth`` hops, filtered to
        ``rel`` if given. Each entry is ``{id, labels, properties}``. An absent
        node returns ``[]``."""
        ...

    def traverse(
        self, start: str, query: str, params: dict[str, Any] | None = None
    ) -> list[dict[str, Any]]:
        """Run a backend-native traversal query from ``start``.

        For Neo4j this is Cypher. Backends that don't support a query language
        may raise ``NotImplementedError`` — callers that need portability
        should stick to ``neighbors``.
        """
        ...

    def delete_node(self, node_id: str) -> None:
        """Remove a node and its incident edges. No-op if missing."""
        ...

    # NOTE: the old ``upsert_fact`` / ``load_facts`` / ``save_facts`` methods
    # are gone. They were not graph operations. Callers that stored FACT
    # memories should use ``add_node`` (entity as node, facts as properties)
    # and ``add_edge`` to link related entities.
