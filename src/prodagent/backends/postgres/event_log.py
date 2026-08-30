"""Postgres-backed ``EventLog``."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

from prodagent.backends._shared.tailing import StreamWakes, tail_stream
from prodagent.backends.postgres._versioned import lock_and_check_version
from prodagent.backends.postgres.schema import ensure_schema_via_pool_async
from prodagent.base.event_log import Event

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from psycopg_pool import AsyncConnectionPool

__all__ = ["PostgresEventLog"]


class PostgresEventLog:
    """Durable, multi-replica ``EventLog`` — per-stream monotonic seq."""

    def __init__(self, pool: AsyncConnectionPool, *, namespace: str = "default") -> None:
        self._pool = pool
        self._ns = namespace
        self._wakes = StreamWakes()

    async def append(self, event: Event, expected_seq: int | None = None) -> int:
        return (await self.append_events([event], expected_seq))[0]

    async def append_events(
        self, events: list[Event], expected_seq: int | None = None
    ) -> list[int]:
        """Group commit: one transaction for the whole batch — the tail
        check locks the max seq per involved stream, seqs run consecutive,
        a single commit makes the batch atomic for other replicas."""
        if not events:
            return []
        await ensure_schema_via_pool_async(self._pool)
        grouped: dict[str, list[Event]] = {}
        for event in events:
            grouped.setdefault(event.stream_id, []).append(event)
        async with self._pool.connection() as conn:
            async with conn.cursor() as cur:
                for stream_id, stream_events in grouped.items():
                    current = await lock_and_check_version(
                        cur,
                        f"{self._ns}:{stream_id}",
                        "SELECT COALESCE(MAX(seq), 0) FROM pa_event "
                        "WHERE namespace = %s AND stream_id = %s",
                        (self._ns, stream_id),
                        expected_seq,
                        f"stream {stream_id}",
                    )
                    rows = []
                    for event in stream_events:
                        event.seq = current + 1
                        current = event.seq
                        rows.append(
                            (
                                self._ns,
                                stream_id,
                                event.seq,
                                json.dumps(
                                    {
                                        "seq": event.seq,
                                        "event_id": event.event_id,
                                        "event_type": str(event.event_type),
                                        "stream_id": event.stream_id,
                                        "version": event.version,
                                        "timestamp": event.timestamp,
                                        "data": event.data,
                                    },
                                    ensure_ascii=False,
                                ),
                            )
                        )
                    # One round trip per stream-batch — the amortization the
                    # buffered tier depends on.
                    await cur.executemany(
                        "INSERT INTO pa_event (namespace, stream_id, seq, payload) "
                        "VALUES (%s, %s, %s, %s::jsonb)",
                        rows,
                    )
            await conn.commit()
        for stream_id in grouped:
            self._wakes.notify(stream_id)
        return [e.seq for e in events]

    async def _decode_rows(self, rows: list[Any]) -> list[Event]:
        out = []
        for row in rows:
            data = json.loads(row[0])
            out.append(
                Event(
                    seq=data["seq"],
                    event_id=data["event_id"],
                    event_type=data["event_type"],
                    stream_id=data["stream_id"],
                    version=data["version"],
                    timestamp=data["timestamp"],
                    data=data["data"],
                )
            )
        return out

    async def get_events(self, stream_id: str) -> list[Event]:
        await ensure_schema_via_pool_async(self._pool)
        async with self._pool.connection() as conn, conn.cursor() as cur:
            await cur.execute(
                "SELECT payload::text FROM pa_event "
                "WHERE namespace = %s AND stream_id = %s ORDER BY seq",
                (self._ns, stream_id),
            )
            rows = await cur.fetchall()
        return await self._decode_rows(rows)

    async def get_after(self, stream_id: str, since_seq: int) -> list[Event]:
        await ensure_schema_via_pool_async(self._pool)
        async with self._pool.connection() as conn, conn.cursor() as cur:
            await cur.execute(
                "SELECT payload::text FROM pa_event "
                "WHERE namespace = %s AND stream_id = %s AND seq > %s ORDER BY seq",
                (self._ns, stream_id, since_seq),
            )
            rows = await cur.fetchall()
        return await self._decode_rows(rows)

    def subscribe(self, stream_id: str, since_seq: int = 0) -> AsyncIterator[Event]:
        # In-process appends wake immediately; cross-replica appends are
        # caught by the poll fallback (LISTEN/NOTIFY is a future upgrade —
        # the suffix law already holds without it).
        return tail_stream(self.get_after, self._wakes, stream_id, since_seq)

    async def replicate(self, events: list[Event]) -> None:
        """Absorb at the events' own seqs — the PK ``(namespace, stream_id,
        seq)`` makes re-shipping a no-op via ON CONFLICT DO NOTHING, one
        round trip for the whole batch."""
        if not events:
            return
        for event in events:
            if event.seq < 1:
                # Event.make leaves seq=0 as the "unassigned" placeholder —
                # shipping one means the source never sequenced it (a wiring
                # bug), and silently skipping it would lose a fact.
                raise ValueError(
                    f"cannot replicate an unsequenced event ({event.event_type} on "
                    f"{event.stream_id}) — append it through a log first"
                )
        await ensure_schema_via_pool_async(self._pool)
        rows = [
            (
                self._ns,
                event.stream_id,
                event.seq,
                json.dumps(
                    {
                        "seq": event.seq,
                        "event_id": event.event_id,
                        "event_type": str(event.event_type),
                        "stream_id": event.stream_id,
                        "version": event.version,
                        "timestamp": event.timestamp,
                        "data": event.data,
                    },
                    ensure_ascii=False,
                ),
            )
            for event in events
        ]
        async with self._pool.connection() as conn:
            async with conn.cursor() as cur:
                await cur.executemany(
                    "INSERT INTO pa_event (namespace, stream_id, seq, payload) "
                    "VALUES (%s, %s, %s, %s::jsonb) "
                    "ON CONFLICT (namespace, stream_id, seq) DO NOTHING",
                    rows,
                )
            await conn.commit()
        for stream_id in {e.stream_id for e in events}:
            self._wakes.notify(stream_id)
