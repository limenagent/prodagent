"""Postgres connection pool construction — single place that reads env vars."""

from __future__ import annotations

import os
from typing import Any

__all__ = ["sync_pool_from_env", "async_pool_from_env", "dsn_from_env"]


def _import_psycopg_pool() -> Any:
    """Import ``psycopg_pool`` or fail with an actionable install hint."""
    try:
        import psycopg_pool
    except ImportError as exc:
        raise ImportError(
            "Postgres backend requires psycopg_pool. "
            "Install it with: pip install 'prodagent[postgres]'"
        ) from exc
    return psycopg_pool


def dsn_from_env() -> str:
    """Build a libpq DSN from ``DATABASE_URL`` or ``PG*`` env vars."""
    url = os.getenv("DATABASE_URL") or os.getenv("POSTGRES_URL")
    if url:
        # psycopg accepts postgres:// or postgresql:// schemes directly.
        return url
    host = os.getenv("PGHOST", os.getenv("POSTGRES_HOST", "localhost"))
    port = os.getenv("PGPORT", os.getenv("POSTGRES_PORT", "5432"))
    user = os.getenv("PGUSER", os.getenv("POSTGRES_USER", "postgres"))
    password = os.getenv("PGPASSWORD", os.getenv("POSTGRES_PASSWORD", ""))
    db = os.getenv("PGDATABASE", os.getenv("POSTGRES_DB", "postgres"))
    return f"host={host} port={port} dbname={db} user={user} password={password}"


def sync_pool_from_env(*, min_size: int = 1, max_size: int = 8) -> Any:
    """A synchronous ``ConnectionPool`` — for sync ports (dead_letter, span,
    document, graph)."""
    psycopg_pool = _import_psycopg_pool()
    return psycopg_pool.ConnectionPool(
        dsn_from_env(), min_size=min_size, max_size=max_size, open=False
    )


def async_pool_from_env(*, min_size: int = 1, max_size: int = 8) -> Any:
    """An async ``AsyncConnectionPool`` — for async ports (checkpoint,
    event_log, cache, approval, lock).
    """
    psycopg_pool = _import_psycopg_pool()
    return psycopg_pool.AsyncConnectionPool(
        conninfo=dsn_from_env(),
        min_size=min_size,
        max_size=max_size,
        open=False,
    )
