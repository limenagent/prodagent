"""Postgres-backed ``CheckpointStore``."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

from prodagent.backends.postgres._versioned import lock_and_check_version
from prodagent.backends.postgres.schema import ensure_schema_via_pool_async
from prodagent.core.exceptions import VersionConflict
from prodagent.core.state.run import AgentRun

if TYPE_CHECKING:
    from psycopg_pool import AsyncConnectionPool

__all__ = ["PostgresCheckpointStore"]


class PostgresCheckpointStore:
    """Versioned, durable, multi-replica ``CheckpointStore``."""

    def __init__(self, pool: AsyncConnectionPool, *, namespace: str = "default") -> None:
        self._pool = pool
        self._ns = namespace

    async def _latest_version(self, run_id: str) -> int:
        await ensure_schema_via_pool_async(self._pool)
        async with self._pool.connection() as conn, conn.cursor() as cur:
            await cur.execute(
                "SELECT COALESCE(MAX(version), 0) FROM pa_checkpoint "
                "WHERE namespace = %s AND run_id = %s",
                (self._ns, run_id),
            )
            row = await cur.fetchone()
            return int(row[0]) if row else 0

    async def save(self, run: AgentRun, expected_version: int | None = None) -> None:
        await ensure_schema_via_pool_async(self._pool)
        async with self._pool.connection() as conn:
            async with conn.cursor() as cur:
                current = await lock_and_check_version(
                    cur,
                    f"{self._ns}:{run.run_id}",
                    "SELECT COALESCE(MAX(version), 0) FROM pa_checkpoint "
                    "WHERE namespace = %s AND run_id = %s",
                    (self._ns, run.run_id),
                    expected_version,
                    f"run {run.run_id}",
                )
                new_version = current + 1
                run.checkpoint_version = new_version
                blob = json.dumps(run.to_dict(), ensure_ascii=False)
                await cur.execute(
                    "INSERT INTO pa_checkpoint (namespace, run_id, version, payload) "
                    "VALUES (%s, %s, %s, %s::jsonb)",
                    (self._ns, run.run_id, new_version, blob),
                )
            await conn.commit()

    async def load(self, run_id: str, version: int | None = None) -> AgentRun | None:
        await ensure_schema_via_pool_async(self._pool)
        if version is None:
            version = await self._latest_version(run_id)
            if version == 0:
                return None
        async with self._pool.connection() as conn, conn.cursor() as cur:
            await cur.execute(
                "SELECT payload::text FROM pa_checkpoint "
                "WHERE namespace = %s AND run_id = %s AND version = %s",
                (self._ns, run_id, version),
            )
            row = await cur.fetchone()
        if row is None:
            return None
        run = AgentRun.from_dict(json.loads(row[0]))
        run.checkpoint_version = version
        return run

    async def list_run_ids(self) -> list[str]:
        await ensure_schema_via_pool_async(self._pool)
        async with self._pool.connection() as conn, conn.cursor() as cur:
            await cur.execute(
                "SELECT DISTINCT run_id FROM pa_checkpoint WHERE namespace = %s ORDER BY run_id",
                (self._ns,),
            )
            rows = await cur.fetchall()
        return [r[0] for r in rows]

    async def list_versions(self, run_id: str) -> list[int]:
        await ensure_schema_via_pool_async(self._pool)
        async with self._pool.connection() as conn, conn.cursor() as cur:
            await cur.execute(
                "SELECT version FROM pa_checkpoint "
                "WHERE namespace = %s AND run_id = %s ORDER BY version",
                (self._ns, run_id),
            )
            rows = await cur.fetchall()
        return [int(r[0]) for r in rows]

    async def fork(
        self,
        run_id: str,
        at_version: int,
        new_run_id: str | None = None,
    ) -> str:
        await ensure_schema_via_pool_async(self._pool)
        if new_run_id is None:
            new_run_id = f"{run_id}-fork-{at_version}"
        async with self._pool.connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    "SELECT payload::text FROM pa_checkpoint "
                    "WHERE namespace = %s AND run_id = %s AND version = %s",
                    (self._ns, run_id, at_version),
                )
                row = await cur.fetchone()
                if row is None:
                    raise KeyError(f"run {run_id} v{at_version} not found")
                await cur.execute(
                    "SELECT COALESCE(MAX(version), 0) FROM pa_checkpoint "
                    "WHERE namespace = %s AND run_id = %s",
                    (self._ns, new_run_id),
                )
                existing_row = await cur.fetchone()
                if existing_row and int(existing_row[0]) != 0:
                    raise VersionConflict(
                        f"fork target run_id={new_run_id} already has checkpoints — "
                        "pass a fresh new_run_id."
                    )
                data: dict[str, Any] = json.loads(row[0])
                data["run_id"] = new_run_id
                new_blob = json.dumps(data, ensure_ascii=False)
                await cur.execute(
                    "INSERT INTO pa_checkpoint (namespace, run_id, version, payload) "
                    "VALUES (%s, %s, 1, %s::jsonb)",
                    (self._ns, new_run_id, new_blob),
                )
            await conn.commit()
        return new_run_id

    async def aclose(self) -> None:
        """Pool is owned by BackendRegistry — nothing for the store to close."""
