"""Shared in-memory adjacency-list graph model for ``FileGraphStore``/``InMemoryGraphStore``."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Iterator


class _Node:
    __slots__ = ("id", "labels", "properties")

    def __init__(self, node_id: str, labels: list[str], properties: dict[str, Any]) -> None:
        self.id = node_id
        self.labels = list(labels)
        self.properties = dict(properties)

    def to_dict(self) -> dict[str, Any]:
        return {"id": self.id, "labels": list(self.labels), "properties": dict(self.properties)}


class _Edge:
    __slots__ = ("dst", "rel", "properties")

    def __init__(self, dst: str, rel: str, properties: dict[str, Any]) -> None:
        self.dst = dst
        self.rel = rel
        self.properties = dict(properties)


class GraphModel:
    """Directed property graph: nodes by id, outgoing edges as an adjacency list."""

    def __init__(self) -> None:
        self.nodes: dict[str, _Node] = {}
        self.out_edges: dict[str, list[_Edge]] = {}

    def add_node(
        self,
        node_id: str,
        labels: list[str] | None = None,
        properties: dict[str, Any] | None = None,
    ) -> None:
        """Insert-or-merge: re-adding a node unions labels and overlays
        properties — recall paths may upsert the same entity many times."""
        existing = self.nodes.get(node_id)
        if existing is None:
            self.nodes[node_id] = _Node(node_id, labels or [], properties or {})
            return
        for lab in labels or []:
            if lab not in existing.labels:
                existing.labels.append(lab)
        existing.properties.update(properties or {})

    def add_edge(
        self,
        src: str,
        dst: str,
        rel: str,
        properties: dict[str, Any] | None = None,
    ) -> None:
        if src not in self.nodes:
            self.add_node(src)
        if dst not in self.nodes:
            self.add_node(dst)
        edges = self.out_edges.setdefault(src, [])
        for e in edges:
            if e.dst == dst and e.rel == rel:
                e.properties.update(properties or {})
                return
        edges.append(_Edge(dst, rel, properties or {}))

    def get_node(self, node_id: str) -> dict[str, Any] | None:
        n = self.nodes.get(node_id)
        return n.to_dict() if n else None

    def list_nodes(self, label: str | None = None) -> list[dict[str, Any]]:
        if label is None:
            return [n.to_dict() for n in self.nodes.values()]
        return [n.to_dict() for n in self.nodes.values() if label in n.labels]

    def neighbors(
        self,
        node_id: str,
        rel: str | None = None,
        depth: int = 1,
    ) -> list[dict[str, Any]]:
        if node_id not in self.nodes:
            return []  # absent start is an empty walk, not an error
        seen: set[str] = {node_id}  # visit guard — also collapses diamond paths
        frontier = [node_id]
        out: list[dict[str, Any]] = []
        for _ in range(depth):
            nxt: list[str] = []
            for n in frontier:
                for e in self.out_edges.get(n, []):
                    if rel is not None and e.rel != rel:
                        continue  # edge filtered: not the relationship being walked
                    if e.dst not in seen:
                        seen.add(e.dst)
                        out.append(self.nodes[e.dst].to_dict())
                        nxt.append(e.dst)
            frontier = nxt  # one BFS level per pass = depth-bounded by construction
        return out

    def delete_node(self, node_id: str) -> None:
        """Remove the node *and* its incident edges in both directions — a
        dangling edge to a missing node would poison every traversal."""
        self.nodes.pop(node_id, None)
        self.out_edges.pop(node_id, None)
        for src, edges in list(self.out_edges.items()):
            self.out_edges[src] = [e for e in edges if e.dst != node_id]
            if not self.out_edges[src]:
                self.out_edges.pop(src)

    def iter_edges(self) -> Iterator[tuple[str, _Edge]]:
        for src, edges in self.out_edges.items():
            for e in edges:
                yield src, e

    def load_from(
        self,
        nodes_data: list[dict[str, Any]],
        edges_data: list[dict[str, Any]],
    ) -> None:
        """Bulk rebuild from the wire shape — the file backend's whole
        persistence story is ``dump to JSONL, load_from back``."""
        for n in nodes_data:
            self.nodes[n["id"]] = _Node(n["id"], n.get("labels", []), n.get("properties", {}))
        for e in edges_data:
            self.out_edges.setdefault(e["src"], []).append(
                _Edge(e["dst"], e["rel"], e.get("properties", {}))
            )
