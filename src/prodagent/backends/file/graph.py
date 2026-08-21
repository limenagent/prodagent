"""File-backed ``GraphStore`` — a real graph persisted to a JSON file."""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

from prodagent.backends._shared.graph_model import GraphModel
from prodagent.backends.file._locking import _exclusive
from prodagent.core.io import write_atomic_json

logger = logging.getLogger(__name__)

__all__ = ["FileGraphStore"]


class FileGraphStore:
    """Directed property graph persisted to ``graph.json``.

    The whole graph loads into memory on init; every mutation rewrites the
    file atomically. Fine for dev and corpora up to a few thousand nodes —
    beyond that, use a graph database (Neo4j).
    """

    def __init__(self, directory: str | Path) -> None:
        self._dir = Path(directory)
        self._dir.mkdir(parents=True, exist_ok=True)
        self._model = GraphModel()
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
        self._model.load_from(data.get("nodes", []), data.get("edges", []))

    def _flush(self) -> None:
        data: dict[str, Any] = {"nodes": [], "edges": []}
        for n in self._model.nodes.values():
            data["nodes"].append(n.to_dict())
        for src, e in self._model.iter_edges():
            data["edges"].append(
                {"src": src, "dst": e.dst, "rel": e.rel, "properties": e.properties}
            )
        write_atomic_json(self._graph_file, data, fsync=False)

    async def add_node(
        self,
        node_id: str,
        labels: list[str] | None = None,
        properties: dict[str, Any] | None = None,
    ) -> None:
        with _exclusive(self._lock_file):
            self._model.add_node(node_id, labels, properties)
            self._flush()

    async def add_edge(
        self,
        src: str,
        dst: str,
        rel: str,
        properties: dict[str, Any] | None = None,
    ) -> None:
        with _exclusive(self._lock_file):
            self._model.add_edge(src, dst, rel, properties)
            self._flush()

    async def get_node(self, node_id: str) -> dict[str, Any] | None:
        return self._model.get_node(node_id)

    async def list_nodes(self, label: str | None = None) -> list[dict[str, Any]]:
        return self._model.list_nodes(label)

    async def neighbors(
        self,
        node_id: str,
        rel: str | None = None,
        depth: int = 1,
    ) -> list[dict[str, Any]]:
        return self._model.neighbors(node_id, rel, depth)

    async def traverse(
        self, start: str, query: str, params: dict[str, Any] | None = None
    ) -> list[dict[str, Any]]:
        raise NotImplementedError(
            "FileGraphStore does not support a query language — use neighbors()"
        )

    async def delete_node(self, node_id: str) -> None:
        with _exclusive(self._lock_file):
            self._model.delete_node(node_id)
            self._flush()
