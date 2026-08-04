"""Postgres session store — single upserted row per session_id."""

from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING

from prodagent.backends.postgres.schema import ensure_schema_via_pool_async
from prodagent.core.exceptions import VersionConflict

if TYPE_CHECKING:
    from psycopg_pool import AsyncConnectionPool

    from prodagent.core.state.session import ConversationSession

logger = logging.getLogger(__name__)

__all__ = ["PostgresSessionStore"]


class PostgresSessionStore:
    """Durable, multi-replica ``SessionStore``."""

    def __init__(self, pool: AsyncConnectionPool, *, namespace: str = "default") -> None:
        self._pool = pool
        self._ns = namespace

    async def save(self, session: ConversationSession, expected_version: int | None = None) -> None:
        """Persist *session*, raising ``VersionConflict`` when a concurrent
        writer landed a newer version first.
        """
        await ensure_schema_via_pool_async(self._pool)
        async with self._pool.connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    "SELECT pg_advisory_xact_lock(hashtext(%s))",
                    (f"{self._ns}:{session.session_id}",),
                )
                await cur.execute(
                    "SELECT version FROM pa_session WHERE namespace = %s AND session_id = %s",
                    (self._ns, session.session_id),
                )
                row = await cur.fetchone()
                current = int(row[0]) if row else 0
                if expected_version is not None and current != expected_version:
                    raise VersionConflict(
                        f"expected version {expected_version} for session {session.session_id}, "
                        f"found {current} — concurrent writer won"
                    )
                session.version = current + 1
                blob = json.dumps(session.to_dict(), ensure_ascii=False)
                await cur.execute(
                    "INSERT INTO pa_session (namespace, session_id, version, payload) "
                    "VALUES (%s, %s, %s, %s::jsonb) "
                    "ON CONFLICT (namespace, session_id) DO UPDATE SET version = EXCLUDED.version, payload = EXCLUDED.payload, saved_at = now()",
                    (self._ns, session.session_id, session.version, blob),
                )
            await conn.commit()

    async def load(self, session_id: str) -> ConversationSession | None:
        from prodagent.core.state.session import ConversationSession

        await ensure_schema_via_pool_async(self._pool)
        async with self._pool.connection() as conn, conn.cursor() as cur:
            await cur.execute(
                "SELECT payload::text, version FROM pa_session "
                "WHERE namespace = %s AND session_id = %s",
                (self._ns, session_id),
            )
            row = await cur.fetchone()
        if row is None:
            return None
        session = ConversationSession.from_dict(json.loads(row[0]))
        session.version = int(row[1])
        return session

    async def aclose(self) -> None:
        """Pool is owned by BackendRegistry — nothing for the store to close."""
