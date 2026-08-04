"""Neo4j-backed ``GraphStore`` — a real graph database.

Neo4j is a purpose-built graph database: nodes, edges, labels, and Cypher
traversal. This is where graph data belongs when it needs to scale across
replicas or when traversal depth makes an in-memory adjacency list
impractical. The default graph backend is ``FileGraphStore`` (no external
service); swap to this for production.

Requires the ``[neo4j]`` extra::

    pip install prodagent[neo4j]
"""

from __future__ import annotations

from prodagent.backends.neo4j.graph import Neo4jGraphStore

__all__ = ["Neo4jGraphStore"]
