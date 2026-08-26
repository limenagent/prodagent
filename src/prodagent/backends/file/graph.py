"""File-backed ``GraphStore`` — a real graph persisted to a JSON file."""

from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Callable

from prodagent.backends._shared.graph_model import GraphModel
from prodagent.backends.file._locking import _exclusive
from prodagent.base.io import write_atomic_json

logger = logging.getLogger(__name__)

__all__ = ["FileGraphStore"]


class FileGraphStore:
    """Directed property graph persisted to ``graph.json``.

    The file is the source of truth: every call re-reads it, so processes
    sharing one directory see each other's writes (atomic replace makes
    unlocked reads safe; writers serialise on the lock file). Fine for dev
    and corpora up to a few thousand nodes — beyond that, use a graph
    database (Neo4j).
    """

    def __init__(self, directory: str | Path) -> None:
        self._dir = Path(directory)
        self._dir.mkdir(parents=True, exist_ok=True)

    @property
    def _graph_file(self) -> Path:
        return self._dir / "graph.json"

    @property
    def _lock_file(self) -> Path:
        return self._dir / "graph.lock"

    def _read_model(self) -> GraphModel:
        model = GraphModel()
        if not self._graph_file.exists():
            return model
        try:
            data = json.loads(self._graph_file.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as exc:
            logger.warning(
                "[graph] read failed for %s: %s — treating as empty", self._graph_file, exc
            )
            return model
        model.load_from(data.get("nodes", []), data.get("edges", []))
        return model

    def _flush_model(self, model: GraphModel) -> None:
        data: dict[str, Any] = {"nodes": [], "edges": []}
        for n in model.nodes.values():
            data["nodes"].append(n.to_dict())
        for src, e in model.iter_edges():
            data["edges"].append(
                {"src": src, "dst": e.dst, "rel": e.rel, "properties": e.properties}
            )
        write_atomic_json(self._graph_file, data, fsync=False)

    def _mutate_sync(self, fn: Callable[[GraphModel], None]) -> None:
        with _exclusive(self._lock_file):
            model = self._read_model()
            fn(model)
            self._flush_model(model)

    async def add_node(
        self,
        node_id: str,
        labels: list[str] | None = None,
        properties: dict[str, Any] | None = None,
    ) -> None:
        await asyncio.to_thread(
            self._mutate_sync, lambda m: m.add_node(node_id, labels, properties)
        )

    async def add_edge(
        self,
        src: str,
        dst: str,
        rel: str,
        properties: dict[str, Any] | None = None,
    ) -> None:
        await asyncio.to_thread(self._mutate_sync, lambda m: m.add_edge(src, dst, rel, properties))

    async def get_node(self, node_id: str) -> dict[str, Any] | None:
        return (await asyncio.to_thread(self._read_model)).get_node(node_id)

    async def list_nodes(self, label: str | None = None) -> list[dict[str, Any]]:
        return (await asyncio.to_thread(self._read_model)).list_nodes(label)

    async def neighbors(
        self,
        node_id: str,
        rel: str | None = None,
        depth: int = 1,
    ) -> list[dict[str, Any]]:
        return (await asyncio.to_thread(self._read_model)).neighbors(node_id, rel, depth)

    async def traverse(
        self, start: str, query: str, params: dict[str, Any] | None = None
    ) -> list[dict[str, Any]]:
        raise NotImplementedError(
            "FileGraphStore does not support a query language — use neighbors()"
        )

    async def delete_node(self, node_id: str) -> None:
        await asyncio.to_thread(self._mutate_sync, lambda m: m.delete_node(node_id))
