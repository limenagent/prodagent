"""Run the port conformance suite against every ``postgres`` backend implementation.

Postgres is a relational database — that is what it is good at. The ports run
against it here are the relational / durable ones: checkpoint, event_log,
document, span. Ephemeral state (cache, lock, approval,
dead_letter) belongs in Redis or memory (see ``test_conformance_redis``);
graph and vector belong in their own dedicated engines.

Requires a running Postgres — set ``DATABASE_URL`` or ``PGHOST``/``PGPORT`` etc.
The whole module is skipped if Postgres is unreachable. Locally we spin one
up via docker on port 5433 (``PGPORT=5433``), using the ``pgvector/pgvector``
image so the ``vector`` extension is available if needed.

Each test gets a unique namespace (its test name) so concurrent test runs on
the same DB do not collide. The namespace's rows are deleted before each test.
"""

from __future__ import annotations

import os
import uuid

import pytest

from prodagent.backends.postgres.checkpoint import PostgresCheckpointStore
from prodagent.backends.postgres.document import PostgresDocumentStore
from prodagent.backends.postgres.event_log import PostgresEventLog
from prodagent.backends.postgres.span import PostgresSpanExporter
from tests.backends.conformance import (
    run_checkpoint_conformance,
    run_checkpoint_fork_conformance,
    run_checkpoint_fork_refuses_existing_conformance,
    run_checkpoint_versioning_conformance,
    run_document_conformance,
    run_document_constraint_storage_conformance,
    run_document_supersede_conformance,
    run_document_touch_conformance,
    run_event_log_batch_conformance,
    run_event_log_batch_expected_seq_conformance,
    run_event_log_conformance,
    run_event_log_empty_plan_conformance,
    run_event_log_plan_isolation_conformance,
    run_event_log_subscribe_conformance,
    run_span_conformance,
    run_span_export_after_shutdown_conformance,
    run_span_shutdown_idempotent_conformance,
)


def _dsn() -> str:
    url = os.getenv("DATABASE_URL") or os.getenv("POSTGRES_URL")
    if url:
        return url
    host = os.getenv("PGHOST", os.getenv("POSTGRES_HOST", "localhost"))
    port = os.getenv("PGPORT", os.getenv("POSTGRES_PORT", "5432"))
    user = os.getenv("PGUSER", os.getenv("POSTGRES_USER", "postgres"))
    password = os.getenv("PGPASSWORD", os.getenv("POSTGRES_PASSWORD", ""))
    db = os.getenv("PGDATABASE", os.getenv("POSTGRES_DB", "postgres"))
    return f"host={host} port={port} dbname={db} user={user} password={password}"


def _ping_pg() -> bool:
    """Use the sync client to avoid event-loop conflicts with pytest-asyncio."""
    try:
        import psycopg

        conn = psycopg.connect(_dsn(), connect_timeout=2)
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT 1")
                return bool(cur.fetchone())
        finally:
            conn.close()
    except Exception:
        return False


pytestmark = pytest.mark.skipif(not _ping_pg(), reason="Postgres not reachable")


@pytest.fixture
async def async_pool():
    from psycopg_pool import AsyncConnectionPool

    pool = AsyncConnectionPool(_dsn(), min_size=1, max_size=4, open=False)
    await pool.open()
    from prodagent.backends.postgres.schema import ensure_schema_async

    async with pool.connection() as conn:
        await ensure_schema_async(conn)
    yield pool
    await pool.close()


@pytest.fixture
def sync_pool():
    from psycopg_pool import ConnectionPool

    pool = ConnectionPool(_dsn(), min_size=1, max_size=4, open=False)
    pool.open()
    from prodagent.backends.postgres.schema import ensure_schema

    with pool.connection() as conn:
        ensure_schema(conn)
    yield pool
    pool.close()


@pytest.fixture
def ns() -> str:
    return f"test-{uuid.uuid4().hex[:8]}"


def _truncate_sync(sync_pool, ns: str) -> None:
    """Delete all rows for this namespace across every table."""
    tables = [
        "pa_checkpoint",
        "pa_event",
        "pa_memory",
        "pa_span",
    ]
    with sync_pool.connection() as conn:
        with conn.cursor() as cur:
            for t in tables:
                cur.execute(f"DELETE FROM {t} WHERE namespace = %s", (ns,))
        conn.commit()


@pytest.fixture
async def clean_async(async_pool, ns):
    """Truncate the namespace's rows before the test runs."""
    async with async_pool.connection() as conn:
        async with conn.cursor() as cur:
            for t in ["pa_checkpoint", "pa_event", "pa_memory"]:
                await cur.execute(f"DELETE FROM {t} WHERE namespace = %s", (ns,))
        await conn.commit()
    return ns


@pytest.fixture
def clean_sync(sync_pool, ns):
    _truncate_sync(sync_pool, ns)
    return ns


# ── checkpoint (async) ────────────────────────────────────────────────────────


async def test_pg_checkpoint_conformance(async_pool, clean_async):
    await run_checkpoint_conformance(
        lambda: PostgresCheckpointStore(async_pool, namespace=clean_async)
    )


async def test_pg_checkpoint_versioning_conformance(async_pool, clean_async):
    await run_checkpoint_versioning_conformance(
        lambda: PostgresCheckpointStore(async_pool, namespace=clean_async)
    )


async def test_pg_checkpoint_fork_conformance(async_pool, clean_async):
    await run_checkpoint_fork_conformance(
        lambda: PostgresCheckpointStore(async_pool, namespace=clean_async)
    )


async def test_pg_checkpoint_fork_refuses_existing_conformance(async_pool, clean_async):
    await run_checkpoint_fork_refuses_existing_conformance(
        lambda: PostgresCheckpointStore(async_pool, namespace=clean_async)
    )


# ── event_log (async) ─────────────────────────────────────────────────────────


async def test_pg_event_log_conformance(async_pool, clean_async):
    await run_event_log_conformance(lambda: PostgresEventLog(async_pool, namespace=clean_async))


async def test_pg_event_log_batch_conformance(async_pool, clean_async):
    await run_event_log_batch_conformance(
        lambda: PostgresEventLog(async_pool, namespace=clean_async)
    )


async def test_pg_event_log_batch_expected_seq_conformance(async_pool, clean_async):
    await run_event_log_batch_expected_seq_conformance(
        lambda: PostgresEventLog(async_pool, namespace=clean_async)
    )


async def test_pg_event_log_subscribe_conformance(async_pool, clean_async):
    await run_event_log_subscribe_conformance(
        lambda: PostgresEventLog(async_pool, namespace=clean_async)
    )


async def test_pg_event_log_plan_isolation_conformance(async_pool, clean_async):
    await run_event_log_plan_isolation_conformance(
        lambda: PostgresEventLog(async_pool, namespace=clean_async)
    )


async def test_pg_event_log_empty_plan_conformance(async_pool, clean_async):
    await run_event_log_empty_plan_conformance(
        lambda: PostgresEventLog(async_pool, namespace=clean_async)
    )


# ── document (sync) ───────────────────────────────────────────────────────────


def test_pg_document_conformance(sync_pool, clean_sync):
    run_document_conformance(lambda: PostgresDocumentStore(sync_pool, namespace=clean_sync))


def test_pg_document_supersede_conformance(sync_pool, clean_sync):
    run_document_supersede_conformance(
        lambda: PostgresDocumentStore(sync_pool, namespace=clean_sync)
    )


def test_pg_document_touch_conformance(sync_pool, clean_sync):
    run_document_touch_conformance(lambda: PostgresDocumentStore(sync_pool, namespace=clean_sync))


def test_pg_document_constraint_storage_conformance(sync_pool, clean_sync):
    run_document_constraint_storage_conformance(
        lambda: PostgresDocumentStore(sync_pool, namespace=clean_sync)
    )


# ── span (sync) ───────────────────────────────────────────────────────────────


async def test_pg_span_conformance(sync_pool, clean_sync):
    await run_span_conformance(lambda: PostgresSpanExporter(sync_pool, namespace=clean_sync))


async def test_pg_span_shutdown_idempotent_conformance(sync_pool, clean_sync):
    await run_span_shutdown_idempotent_conformance(
        lambda: PostgresSpanExporter(sync_pool, namespace=clean_sync)
    )


async def test_pg_span_export_after_shutdown_conformance(sync_pool, clean_sync):
    await run_span_export_after_shutdown_conformance(
        lambda: PostgresSpanExporter(sync_pool, namespace=clean_sync)
    )
