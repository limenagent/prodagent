"""Postgres-backed ``DocumentStore``."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

from prodagent.backends._shared.document_write import build_stored_memory
from prodagent.core.time import now_timestamp
from prodagent.ports.document import (
    MAX_SOFT_MEMORIES,
    MemoryRecord,
    MemoryType,
    StoredMemory,
)

if TYPE_CHECKING:
    from collections.abc import Callable

    from psycopg_pool import ConnectionPool

__all__ = ["PostgresDocumentStore"]


class PostgresDocumentStore:
    """Distributed ``DocumentStore`` — JSON blobs in Postgres, keyed by mem_id."""

    def __init__(self, pool: ConnectionPool, *, namespace: str = "default") -> None:
        self._pool = pool
        self._ns = namespace

    def _load_all(self) -> list[StoredMemory]:
        with self._pool.connection() as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT payload::text FROM pa_memory WHERE namespace = %s "
                "ORDER BY (payload->>'created_at') DESC",
                (self._ns,),
            )
            rows = cur.fetchall()
        return [StoredMemory.from_dict(json.loads(r[0])) for r in rows]

    async def load_memories(self) -> list[StoredMemory]:
        return self._load_all()

    async def load_constraints(self) -> list[StoredMemory]:
        return [m for m in self._load_all() if m.memory_type is MemoryType.CONSTRAINT]

    async def save_memories(self, data: list[StoredMemory]) -> None:
        with self._pool.connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "DELETE FROM pa_memory WHERE namespace = %s",
                    (self._ns,),
                )
                for m in data[:MAX_SOFT_MEMORIES]:
                    blob = json.dumps(m.to_dict(), ensure_ascii=False)
                    cur.execute(
                        "INSERT INTO pa_memory (namespace, mem_id, payload) "
                        "VALUES (%s, %s, %s::jsonb) "
                        "ON CONFLICT (namespace, mem_id) DO UPDATE SET payload = EXCLUDED.payload",
                        (self._ns, m.id, blob),
                    )
            conn.commit()

    async def append_soft(self, record: MemoryRecord) -> None:
        stored = build_stored_memory(record)
        mid = stored.id

        blob = json.dumps(stored.to_dict(), ensure_ascii=False)
        with self._pool.connection() as conn, conn.cursor() as cur:
            cur.execute(
                "INSERT INTO pa_memory (namespace, mem_id, payload) "
                "VALUES (%s, %s, %s::jsonb) "
                "ON CONFLICT (namespace, mem_id) DO UPDATE SET payload = EXCLUDED.payload",
                (self._ns, mid, blob),
            )
            # Bounded soft memory: evict oldest beyond MAX_SOFT_MEMORIES.
            cur.execute(
                "DELETE FROM pa_memory WHERE namespace = %s AND mem_id IN ("
                "  SELECT mem_id FROM pa_memory WHERE namespace = %s "
                "  ORDER BY (payload->>'created_at') DESC OFFSET %s"
                ")",
                (self._ns, self._ns, MAX_SOFT_MEMORIES),
            )
        conn.commit()

    def _mutate_mem(self, mem_id: str, fn: Callable[[dict[str, Any]], None]) -> None:
        """Load one memory's payload, apply fn in place, write it back."""
        with self._pool.connection() as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT payload::text FROM pa_memory WHERE namespace = %s AND mem_id = %s",
                (self._ns, mem_id),
            )
            row = cur.fetchone()
            if row is None:
                return
            data: dict[str, Any] = json.loads(row[0])
            fn(data)
            blob = json.dumps(data, ensure_ascii=False)
            cur.execute(
                "UPDATE pa_memory SET payload = %s::jsonb WHERE namespace = %s AND mem_id = %s",
                (blob, self._ns, mem_id),
            )
        conn.commit()

    async def mark_superseded(self, mem_id: str, superseded: bool) -> None:
        self._mutate_mem(mem_id, lambda d: d.__setitem__("superseded", superseded))

    async def touch_memory(self, mem_id: str) -> None:
        def _touch(d: dict[str, Any]) -> None:
            d["access_count"] = d.get("access_count", 0) + 1
            d["last_access"] = now_timestamp()

        self._mutate_mem(mem_id, _touch)
