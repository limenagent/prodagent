"""File-backed ``GraphStore`` — a real graph persisted to a JSON file."""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

from prodagent.backends.file._locking import _exclusive
from prodagent.core.io import write_atomic_json

logger = logging.getLogger(__name__)

__all__ = ["FileGraphStore"]


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


class FileGraphStore:
    """Directed property graph persisted to ``graph.json``.

    The whole graph loads into memory on init; every mutation rewrites the
    file atomically. Fine for dev and corpora up to a few thousand nodes —
    beyond that, use a graph database (Neo4j).
    """

    def __init__(self, directory: str | Path) -> None:
        self._dir = Path(directory)
        self._dir.mkdir(parents=True, exist_ok=True)
        self._nodes: dict[str, _Node] = {}
        self._out: dict[str, list[_Edge]] = {}
        self._load()

    @property
    def _graph_file(self) -> Path:
        return self._dir / "graph.json"

    @property
    def _lock_file(self) -> Path:
        return self._dir / "graph.lock"

    def _load(self) -> None:
        if not self._graph_file.exists():
            return
        try:
            data = json.loads(self._graph_file.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as exc:
            logger.warning(
                "[graph] read failed for %s: %s — treating as empty", self._graph_file, exc
            )
            return
        for n in data.get("nodes", []):
            self._nodes[n["id"]] = _Node(n["id"], n.get("labels", []), n.get("properties", {}))
        for e in data.get("edges", []):
            self._out.setdefault(e["src"], []).append(
                _Edge(e["dst"], e["rel"], e.get("properties", {}))
            )

    def _flush(self) -> None:
        data: dict[str, Any] = {"nodes": [], "edges": []}
        for n in self._nodes.values():
            data["nodes"].append(n.to_dict())
        for src, edges in self._out.items():
            for e in edges:
                data["edges"].append(
                    {"src": src, "dst": e.dst, "rel": e.rel, "properties": e.properties}
                )
        write_atomic_json(self._graph_file, data, fsync=False)

    def add_node(
        self,
        node_id: str,
        labels: list[str] | None = None,
        properties: dict[str, Any] | None = None,
    ) -> None:
        with _exclusive(self._lock_file):
            existing = self._nodes.get(node_id)
            if existing is None:
                self._nodes[node_id] = _Node(node_id, labels or [], properties or {})
            else:
                for lab in labels or []:
                    if lab not in existing.labels:
                        existing.labels.append(lab)
                existing.properties.update(properties or {})
            self._flush()

    def add_edge(
        self,
        src: str,
        dst: str,
        rel: str,
        properties: dict[str, Any] | None = None,
    ) -> None:
        with _exclusive(self._lock_file):
            if src not in self._nodes:
                self._nodes[src] = _Node(src, [], {})
            if dst not in self._nodes:
                self._nodes[dst] = _Node(dst, [], {})
            edges = self._out.setdefault(src, [])
            for e in edges:
                if e.dst == dst and e.rel == rel:
                    e.properties.update(properties or {})
                    self._flush()
                    return
            edges.append(_Edge(dst, rel, properties or {}))
            self._flush()

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
                for e in self._out.get(n, []):
                    if rel is not None and e.rel != rel:
                        continue
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
            "FileGraphStore does not support a query language — use neighbors()"
        )

    def delete_node(self, node_id: str) -> None:
        with _exclusive(self._lock_file):
            self._nodes.pop(node_id, None)
            self._out.pop(node_id, None)
            for src, edges in list(self._out.items()):
                self._out[src] = [e for e in edges if e.dst != node_id]
                if not self._out[src]:
                    self._out.pop(src)
            self._flush()
