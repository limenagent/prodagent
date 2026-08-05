"""FactStore — reads/writes ``Fact``-labeled nodes on behalf of ``MemoryManager``."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from prodagent.cognition.memory.storage import MemoryType, StoredMemory, mem_id
from prodagent.core.time import now_timestamp

if TYPE_CHECKING:
    from prodagent.cognition.memory.storage import MemoryRecord
    from prodagent.ports.graph import GraphStore

__all__ = ["FactStore"]


class FactStore:
    def __init__(self, facts: GraphStore) -> None:
        self._facts = facts

    def get_node(self, node_id: str) -> dict[str, Any] | None:
        return self._facts.get_node(node_id)

    def add_node(
        self,
        node_id: str,
        labels: list[str] | None = None,
        properties: dict[str, Any] | None = None,
    ) -> None:
        self._facts.add_node(node_id, labels=labels, properties=properties)

    def list_nodes(self, label: str | None = None) -> list[dict[str, Any]]:
        return self._facts.list_nodes(label=label)

    def write(self, record: MemoryRecord) -> None:
        eid = record.entity_id or mem_id(record.content)
        existing = self.get_node(eid)
        version = (existing["properties"].get("version", 0) + 1) if existing else 1
        self.add_node(
            eid,
            labels=["Fact"],
            properties={
                "content": record.content,
                "entity_id": eid,
                "domain": record.domain,
                "source": record.source,
                "version": version,
                "created_at": now_timestamp(),
                "embedding": record.embedding,
            },
        )

    def load_all(self) -> list[StoredMemory]:
        out: list[StoredMemory] = []
        for node in self.list_nodes(label="Fact"):
            p = node["properties"]
            out.append(
                StoredMemory(
                    id=node["id"],
                    content=p.get("content", ""),
                    memory_type=MemoryType.FACT,
                    domain=p.get("domain", "general"),
                    entity_id=node["id"],
                    created_at=p.get("created_at", ""),
                    version=p.get("version", 1),
                    embedding=p.get("embedding"),
                )
            )
        return out
