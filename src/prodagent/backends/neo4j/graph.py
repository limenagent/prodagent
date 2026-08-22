"""Neo4j ``GraphStore`` — Cypher-native graph traversal."""

from __future__ import annotations

from typing import Any

__all__ = ["Neo4jGraphStore"]


def _cypher_escape(label: str) -> str:
    """Escape a label/rel type for Cypher — backtick-wrap, escape inner backticks."""
    return "`" + label.replace("`", "``") + "`"


def _shape_node(rec: Any) -> dict[str, Any]:
    """Render a Neo4j record as a node dict — drop synthetic Entity label and id prop."""
    labels = [lab for lab in rec["labels"] if lab != "Entity"]
    props = {k: v for k, v in rec["props"].items() if k != "id"}
    return {"id": rec["id"], "labels": labels, "properties": props}


class Neo4jGraphStore:
    """Directed property graph on Neo4j, queried via Cypher."""

    def __init__(self, uri: str, user: str, password: str) -> None:
        try:
            from neo4j import GraphDatabase
        except ImportError as exc:
            raise ImportError(
                "Neo4j backend requires the neo4j package. "
                "Install it with: pip install 'prodagent[neo4j]'"
            ) from exc

        self._driver: Any = GraphDatabase.driver(uri, auth=(user, password))
        self._driver.verify_connectivity()

    def close(self) -> None:
        self._driver.close()

    async def add_node(
        self,
        node_id: str,
        labels: list[str] | None = None,
        properties: dict[str, Any] | None = None,
    ) -> None:
        labels = labels or []
        props = {"id": node_id, **(properties or {})}
        label_set_clauses = ", ".join(f"n:{_cypher_escape(lab)}" for lab in labels)
        set_clause = f"SET n += $props{', ' + label_set_clauses if labels else ''}"
        cypher = f"MERGE (n:Entity {{id: $id}}) {set_clause}"
        with self._driver.session() as sess:
            sess.run(cypher, id=node_id, props=props)

    async def add_edge(
        self,
        src: str,
        dst: str,
        rel: str,
        properties: dict[str, Any] | None = None,
    ) -> None:
        rel_esc = _cypher_escape(rel)
        props = properties or {}
        # Auto-create endpoints (MERGE on id alone, with Entity label).
        cypher = (
            "MERGE (a:Entity {id: $src}) "
            "MERGE (b:Entity {id: $dst}) "
            f"MERGE (a)-[r:{rel_esc}]->(b) "
            "SET r += $props"
        )
        with self._driver.session() as sess:
            sess.run(cypher, src=src, dst=dst, props=props)

    async def get_node(self, node_id: str) -> dict[str, Any] | None:
        with self._driver.session() as sess:
            rec = sess.run(
                "MATCH (n:Entity {id: $id}) "
                "RETURN n.id AS id, labels(n) AS labels, properties(n) AS props",
                id=node_id,
            ).single()
        if rec is None:
            return None
        return _shape_node(rec)

    async def list_nodes(self, label: str | None = None) -> list[dict[str, Any]]:
        if label is None:
            cypher = (
                "MATCH (n:Entity) RETURN n.id AS id, labels(n) AS labels, properties(n) AS props"
            )
            params: dict[str, Any] = {}
        else:
            # Filter by a caller label (not Entity).
            lab = _cypher_escape(label)
            cypher = (
                f"MATCH (n:Entity:{lab}) "
                "RETURN n.id AS id, labels(n) AS labels, properties(n) AS props"
            )
            params = {}
        with self._driver.session() as sess:
            records = sess.run(cypher, **params)
            return [_shape_node(rec) for rec in records]

    async def neighbors(
        self,
        node_id: str,
        rel: str | None = None,
        depth: int = 1,
    ) -> list[dict[str, Any]]:
        rel_pattern = "" if rel is None else f":{_cypher_escape(rel)}"
        # variable-length path 1..depth hops, only out-edges
        cypher = (
            "MATCH (start:Entity {id: $id})-"
            f"[r{rel_pattern}*1..{int(depth)}]->(m:Entity) "
            "WHERE m.id <> $id "
            "WITH DISTINCT m "
            "RETURN m.id AS id, labels(m) AS labels, properties(m) AS props"
        )
        with self._driver.session() as sess:
            records = sess.run(cypher, id=node_id)
            return [_shape_node(rec) for rec in records]

    async def traverse(
        self, start: str, query: str, params: dict[str, Any] | None = None
    ) -> list[dict[str, Any]]:
        wrapped = f"MATCH (start:Entity {{id: $start_id}}) WITH start {query}"
        with self._driver.session() as sess:
            records = sess.run(wrapped, start_id=start, **(params or {}))
            return [dict(rec) for rec in records]

    async def delete_node(self, node_id: str) -> None:
        # DETACH DELETE drops the node and its incident edges in one go.
        with self._driver.session() as sess:
            sess.run("MATCH (n:Entity {id: $id}) DETACH DELETE n", id=node_id)
