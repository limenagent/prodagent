"""Backend connection-pool registry — one home for shared clients."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from prodagent.core.config import FrameworkConfig


class BackendRegistry:
    """Per-``FrameworkConfig`` lazy cache of shared backend clients."""

    __slots__ = (
        "_redis_async",
        "_redis_sync",
        "_pg_async_pool",
        "_pg_sync_pool",
        "_close_lock",
    )

    def __init__(self) -> None:
        self._redis_async: Any = None
        self._redis_sync: Any = None
        self._pg_async_pool: Any = None
        self._pg_sync_pool: Any = None
        self._close_lock = asyncio.Lock()

    @classmethod
    def for_config(cls, fw: FrameworkConfig) -> BackendRegistry:
        reg = fw._backend_registry
        if reg is None:
            reg = cls()
            fw._backend_registry = reg
        return reg

    def redis_async_client(self) -> Any:
        if self._redis_async is None:
            from prodagent.backends.redis import redis_client_from_env

            self._redis_async = redis_client_from_env(async_=True)
        return self._redis_async

    def redis_sync_client(self) -> Any:
        if self._redis_sync is None:
            from prodagent.backends.redis import redis_client_from_env

            self._redis_sync = redis_client_from_env()
        return self._redis_sync

    def pg_async_pool(self) -> Any:
        if self._pg_async_pool is None:
            from prodagent.backends.postgres import async_pool_from_env

            self._pg_async_pool = async_pool_from_env()
        return self._pg_async_pool

    def pg_sync_pool(self) -> Any:
        if self._pg_sync_pool is None:
            from prodagent.backends.postgres import ensure_schema_via_pool, sync_pool_from_env

            pool = sync_pool_from_env()
            ensure_schema_via_pool(pool)
            self._pg_sync_pool = pool
        return self._pg_sync_pool

    async def aclose(self) -> None:
        """Close every lazily-created client/pool. Idempotent and concurrency-safe.

        The registry is the single owner of these shared clients — stores that
        borrow a pool must not close it, or closing one store would break every
        sibling sharing the same pool. Call this once at shutdown (the
        playground's ``RunRegistry.aclose`` does).

        After close, lazily-created clients are dropped to ``None``; calling
        any ``*_client()`` / ``*_pool()`` again builds a fresh one. Stores
        that already hold a reference to a closed client will fail on next
        use — close is a shutdown signal, not a mid-run rotation.
        """
        async with self._close_lock:
            if self._redis_async is not None:
                await self._redis_async.aclose()
                self._redis_async = None
            if self._redis_sync is not None:
                self._redis_sync.close()
                self._redis_sync = None
            if self._pg_async_pool is not None:
                await self._pg_async_pool.close()
                self._pg_async_pool = None
            if self._pg_sync_pool is not None:
                self._pg_sync_pool.close()
                self._pg_sync_pool = None

    def close(self) -> None:
        """Sync close for the sync-only clients (Redis sync, pg sync pool)."""
        if self._redis_sync is not None:
            self._redis_sync.close()
            self._redis_sync = None
        if self._pg_sync_pool is not None:
            self._pg_sync_pool.close()
            self._pg_sync_pool = None
