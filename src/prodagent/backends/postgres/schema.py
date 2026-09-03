"""Schema for the postgres backend."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from psycopg import AsyncConnection, Connection

__all__ = ["ensure_schema", "ensure_schema_async", "SCHEMA_SQL"]

# One big SQL block — executed as a single script. Idempotent: tables and
# indexes use IF NOT EXISTS, so re-running on a matching DB is a no-op. The
# DO block below is a targeted migration for the ``plan_id`` -> ``stream_id``
# rename in ``pa_event`` — it fires only while the old column is still present,
# then leaves no trace.
SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS pa_checkpoint (
    namespace  text NOT NULL,
    run_id     text NOT NULL,
    version    integer NOT NULL,
    payload    jsonb NOT NULL,
    saved_at   timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (namespace, run_id, version)
);
CREATE INDEX IF NOT EXISTS pa_checkpoint_versions_idx
    ON pa_checkpoint (namespace, run_id, version);

CREATE TABLE IF NOT EXISTS pa_event (
    namespace  text NOT NULL,
    stream_id  text NOT NULL,
    seq        integer NOT NULL,
    payload    jsonb NOT NULL,
    appended_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (namespace, stream_id, seq)
);
-- Migration: databases created before the ``plan_id`` -> ``stream_id`` rename
-- already have a ``pa_event`` table, so the ``CREATE TABLE IF NOT EXISTS``
-- above is a no-op on them and their column keeps the old name. Rename it in
-- place (rows are preserved) so the index below can reference ``stream_id``.
DO $$
BEGIN
    IF EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_schema = current_schema()
          AND table_name = 'pa_event'
          AND column_name = 'plan_id'
    ) AND NOT EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_schema = current_schema()
          AND table_name = 'pa_event'
          AND column_name = 'stream_id'
    ) THEN
        ALTER TABLE pa_event RENAME COLUMN plan_id TO stream_id;
    END IF;
END $$;
DROP INDEX IF EXISTS pa_event_plan_idx;
CREATE INDEX IF NOT EXISTS pa_event_stream_idx ON pa_event (namespace, stream_id, seq);

CREATE TABLE IF NOT EXISTS pa_memory (
    namespace  text NOT NULL,
    mem_id     text NOT NULL,
    payload    jsonb NOT NULL,
    PRIMARY KEY (namespace, mem_id)
);

CREATE TABLE IF NOT EXISTS pa_span (
    namespace   text NOT NULL,
    id          bigserial PRIMARY KEY,
    payload     jsonb NOT NULL,
    exported_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS pa_span_ns_idx ON pa_span (namespace);

CREATE TABLE IF NOT EXISTS pa_session (
    namespace   text NOT NULL,
    session_id  text NOT NULL,
    version     integer NOT NULL,
    payload     jsonb NOT NULL,
    saved_at    timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (namespace, session_id)
);
"""


def ensure_schema(conn: Connection) -> None:
    """Sync: create all tables if missing. Safe to call on every store init."""
    with conn.cursor() as cur:
        cur.execute(SCHEMA_SQL)
    conn.commit()


async def ensure_schema_async(conn: AsyncConnection) -> None:
    """Async variant — same schema, async cursor."""
    async with conn.cursor() as cur:
        await cur.execute(SCHEMA_SQL)
    await conn.commit()


def ensure_schema_via_pool(pool: Any) -> None:
    """Open the pool (if not open), then run ``ensure_schema`` on a conn."""
    if not pool._opened:  # noqa: SLF001 — psycopg_pool uses _opened
        pool.open()
    with pool.connection() as conn:
        ensure_schema(conn)


async def ensure_schema_via_pool_async(pool: Any) -> None:
    """Open the async pool (if not open), then run ``ensure_schema_async``."""
    if getattr(pool, "_prodagent_schema_ready", False):
        return
    # AsyncConnectionPool.open() is a coroutine; check the internal flag first.
    if not pool._opened:  # noqa: SLF001
        await pool.open()
    async with pool.connection() as conn:
        await ensure_schema_async(conn)
    object.__setattr__(pool, "_prodagent_schema_ready", True)
