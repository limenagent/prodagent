"""Postgres-backed ``EventLog``."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

from prodagent.backends.postgres.schema import ensure_schema_via_pool_async
from prodagent.core.event_log import Event
from prodagent.core.exceptions import VersionConflict

if TYPE_CHECKING:
    from psycopg_pool import AsyncConnectionPool

__all__ = ["PostgresEventLog"]


class PostgresEventLog:
    """Durable, multi-replica ``EventLog`` — per-plan monotonic seq."""

    def __init__(self, pool: AsyncConnectionPool, *, namespace: str = "default") -> None:
        self._pool = pool
        self._ns = namespace

    async def append(self, event: Event, expected_seq: int | None = None) -> int:
        await ensure_schema_via_pool_async(self._pool)
        record: dict[str, Any] = {
            "seq": event.seq,
            "event_id": event.event_id,
            "event_type": str(event.event_type),
            "plan_id": event.plan_id,
            "version": event.version,
            "timestamp": event.timestamp,
            "data": event.data,
        }
        async with self._pool.connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    "SELECT pg_advisory_xact_lock(hashtext(%s))",
                    (f"{self._ns}:{event.plan_id}",),
                )
                await cur.execute(
                    "SELECT COALESCE(MAX(seq), 0) FROM pa_event "
                    "WHERE namespace = %s AND plan_id = %s",
                    (self._ns, event.plan_id),
                )
                row = await cur.fetchone()
                current = int(row[0]) if row else 0
                if expected_seq is not None and current != expected_seq:
                    raise VersionConflict(
                        f"expected tail seq {expected_seq} for plan {event.plan_id}, "
                        f"found {current} — concurrent writer won"
                    )
                new_seq = current + 1
                event.seq = new_seq
                record["seq"] = new_seq
                blob = json.dumps(record, ensure_ascii=False)
                await cur.execute(
                    "INSERT INTO pa_event (namespace, plan_id, seq, payload) "
                    "VALUES (%s, %s, %s, %s::jsonb)",
                    (self._ns, event.plan_id, new_seq, blob),
                )
            await conn.commit()
        return new_seq

    async def _decode_rows(self, rows: list[Any]) -> list[Event]:
        out = []
        for row in rows:
            data = json.loads(row[0])
            out.append(
                Event(
                    seq=data["seq"],
                    event_id=data["event_id"],
                    event_type=data["event_type"],
                    plan_id=data["plan_id"],
                    version=data["version"],
                    timestamp=data["timestamp"],
                    data=data["data"],
                )
            )
        return out

    async def get_events(self, plan_id: str) -> list[Event]:
        await ensure_schema_via_pool_async(self._pool)
        async with self._pool.connection() as conn, conn.cursor() as cur:
            await cur.execute(
                "SELECT payload::text FROM pa_event "
                "WHERE namespace = %s AND plan_id = %s ORDER BY seq",
                (self._ns, plan_id),
            )
            rows = await cur.fetchall()
        return await self._decode_rows(rows)

    async def get_after(self, plan_id: str, since_seq: int) -> list[Event]:
        await ensure_schema_via_pool_async(self._pool)
        async with self._pool.connection() as conn, conn.cursor() as cur:
            await cur.execute(
                "SELECT payload::text FROM pa_event "
                "WHERE namespace = %s AND plan_id = %s AND seq > %s ORDER BY seq",
                (self._ns, plan_id, since_seq),
            )
            rows = await cur.fetchall()
        return await self._decode_rows(rows)
