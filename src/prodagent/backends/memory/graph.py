"""In-process ``GraphStore`` — adjacency list, no persistence."""

from __future__ import annotations

from collections import defaultdict
from typing import Any

__all__ = ["InMemoryGraphStore"]


class _Node:
    __slots__ = ("id", "labels", "properties")

    def __init__(self, node_id: str, labels: list[str], properties: dict[str, Any]) -> None:
        self.id = node_id
        self.labels = list(labels)
        self.properties = dict(properties)

    def to_dict(self) -> dict[str, Any]:
        return {"id": self.id, "labels": list(self.labels), "properties": dict(self.properties)}


class _Edge:
    __slots__ = ("src", "dst", "rel", "properties")

    def __init__(self, src: str, dst: str, rel: str, properties: dict[str, Any]) -> None:
        self.src = src
        self.dst = dst
        self.rel = rel
        self.properties = dict(properties)


class InMemoryGraphStore:
    """Directed property graph in a dict — BFS for ``neighbors``."""

    def __init__(self) -> None:
        self._nodes: dict[str, _Node] = {}
        # out-edges: (src, rel) -> list of (dst, properties)
        self._out: dict[tuple[str, str], list[_Edge]] = defaultdict(list)

    def add_node(
        self,
        node_id: str,
        labels: list[str] | None = None,
        properties: dict[str, Any] | None = None,
    ) -> None:
        existing = self._nodes.get(node_id)
        if existing is None:
            self._nodes[node_id] = _Node(node_id, labels or [], properties or {})
            return
        # merge — labels union, properties update
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
        if src not in self._nodes:
            self.add_node(src)
        if dst not in self._nodes:
            self.add_node(dst)
        key = (src, rel)
        for e in self._out[key]:
            if e.dst == dst:
                e.properties.update(properties or {})  # idempotent merge
                return
        self._out[key].append(_Edge(src, dst, rel, properties or {}))

    def get_node(self, node_id: str) -> dict[str, Any] | None:
        n = self._nodes.get(node_id)
        return n.to_dict() if n else None

    def list_nodes(self, label: str | None = None) -> list[dict[str, Any]]:
        if label is None:
            return [n.to_dict() for n in self._nodes.values()]
        return [n.to_dict() for n in self._nodes.values() if label in n.labels]

    def neighbors(
        self,
        node_id: str,
        rel: str | None = None,
        depth: int = 1,
    ) -> list[dict[str, Any]]:
        if node_id not in self._nodes:
            return []
        seen: set[str] = {node_id}
        frontier = [node_id]
        out: list[dict[str, Any]] = []
        for _ in range(depth):
            nxt: list[str] = []
            for n in frontier:
                if rel is not None:
                    for e in self._out.get((n, rel), ()):
                        if e.dst not in seen:
                            seen.add(e.dst)
                            out.append(self._nodes[e.dst].to_dict())
                            nxt.append(e.dst)
                else:
                    for (src, _r), edges in self._out.items():
                        if src != n:
                            continue
                        for e in edges:
                            if e.dst not in seen:
                                seen.add(e.dst)
                                out.append(self._nodes[e.dst].to_dict())
                                nxt.append(e.dst)
            frontier = nxt
        return out

    def traverse(
        self, start: str, query: str, params: dict[str, Any] | None = None
    ) -> list[dict[str, Any]]:
        raise NotImplementedError(
            "InMemoryGraphStore does not support a query language — use neighbors()"
        )

    def delete_node(self, node_id: str) -> None:
        self._nodes.pop(node_id, None)
        # drop out-edges from this node
        for key in [k for k in self._out if k[0] == node_id]:
            self._out.pop(key, None)
        # drop in-edges to this node
        for key, edges in list(self._out.items()):
            self._out[key] = [e for e in edges if e.dst != node_id]
            if not self._out[key]:
                self._out.pop(key)
