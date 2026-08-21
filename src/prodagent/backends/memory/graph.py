"""In-process ``GraphStore`` — adjacency list, no persistence."""

from __future__ import annotations

from typing import Any

from prodagent.backends._shared.graph_model import GraphModel

__all__ = ["InMemoryGraphStore"]


class InMemoryGraphStore:
    """Directed property graph in a dict — BFS for ``neighbors``."""

    def __init__(self) -> None:
        self._model = GraphModel()

    async def add_node(
        self,
        node_id: str,
        labels: list[str] | None = None,
        properties: dict[str, Any] | None = None,
    ) -> None:
        self._model.add_node(node_id, labels, properties)

    async def add_edge(
        self,
        src: str,
        dst: str,
        rel: str,
        properties: dict[str, Any] | None = None,
    ) -> None:
        self._model.add_edge(src, dst, rel, properties)

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
            "InMemoryGraphStore does not support a query language — use neighbors()"
        )

    async def delete_node(self, node_id: str) -> None:
        self._model.delete_node(node_id)
