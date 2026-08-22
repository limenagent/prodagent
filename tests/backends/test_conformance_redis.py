"""Run the port conformance suite against the ``redis`` backend.

Redis is a KV + TTL engine — that is what it is good at. The ports run against
it here are the ephemeral / in-flight ones: cache, lock, dead_letter.
Relational state (checkpoint, event_log, document, span) and typed state
(graph) do NOT belong in Redis — those live in ``test_conformance_postgres``
/ ``test_conformance_neo4j`` respectively.

Requires a running Redis — set ``REDIS_URL`` or ``REDIS_HOST``/``REDIS_PORT``.
The whole module is skipped if Redis is unreachable. Locally we run docker
on port 6390.

Each test gets a unique namespace (its test name) so concurrent test runs on
the same Redis do not collide. The namespace is flushed before each test.
"""

from __future__ import annotations

import os
import uuid

import pytest

from prodagent.backends.redis.cache import RedisCache
from prodagent.backends.redis.dead_letter import RedisDeadLetterQueue
from prodagent.backends.redis.lock import RedisLockStore
from tests.backends.conformance import (
    run_cache_conformance,
    run_cache_key_isolation_conformance,
    run_dead_letter_conformance,
    run_dead_letter_escalation_conformance,
    run_dead_letter_message_isolation_conformance,
    run_lock_conformance,
    run_lock_mutual_exclusion_conformance,
    run_lock_nonblocking_tryacquire_conformance,
    run_lock_release_idempotent_conformance,
)


def _redis_url() -> str:
    if os.getenv("REDIS_URL"):
        return os.environ["REDIS_URL"]
    return f"redis://{os.getenv('REDIS_HOST', 'localhost')}:{os.getenv('REDIS_PORT', '6379')}/{os.getenv('REDIS_DB', '0')}"


def _ping_redis() -> bool:
    """Use the sync client to avoid creating/closing an event loop at import
    time, which conflicts with pytest-asyncio's loop management."""
    try:
        from redis import Redis

        client = Redis.from_url(_redis_url(), decode_responses=False)
        try:
            return bool(client.ping())
        finally:
            client.close()
    except Exception:
        return False


# Skip the whole module if Redis is not reachable.
pytestmark = pytest.mark.skipif(not _ping_redis(), reason="Redis not reachable")


@pytest.fixture
async def async_client():
    from redis.asyncio import Redis

    client = Redis.from_url(_redis_url(), decode_responses=False)
    yield client
    await client.aclose()


@pytest.fixture
def sync_client():
    from redis import Redis

    client = Redis.from_url(_redis_url(), decode_responses=False)
    yield client
    client.close()


@pytest.fixture
def ns() -> str:
    return f"test-{uuid.uuid4().hex[:8]}"


@pytest.fixture
async def clean_async(async_client, ns):
    """Flush the namespace's keys before the test runs (async, in-loop)."""
    async for key in async_client.scan_iter(f"prodagent:{ns}:*"):
        await async_client.delete(key)
    return ns


@pytest.fixture
def clean_sync(sync_client, ns):
    def _flush():
        for key in sync_client.scan_iter(f"prodagent:{ns}:*"):
            sync_client.delete(key)

    _flush()
    return ns


# ── cache (async) ─────────────────────────────────────────────────────────────


async def test_redis_cache_conformance(async_client, clean_async):
    await run_cache_conformance(lambda: RedisCache(async_client, namespace=clean_async))


async def test_redis_cache_key_isolation_conformance(async_client, clean_async):
    await run_cache_key_isolation_conformance(
        lambda: RedisCache(async_client, namespace=clean_async)
    )


# ── lock (async) ──────────────────────────────────────────────────────────────


async def test_redis_lock_conformance(async_client, clean_async):
    await run_lock_conformance(lambda: RedisLockStore(async_client, namespace=clean_async))


async def test_redis_lock_mutual_exclusion_conformance(async_client, clean_async):
    await run_lock_mutual_exclusion_conformance(
        lambda: RedisLockStore(async_client, namespace=clean_async)
    )


async def test_redis_lock_release_idempotent_conformance(async_client, clean_async):
    await run_lock_release_idempotent_conformance(
        lambda: RedisLockStore(async_client, namespace=clean_async)
    )


async def test_redis_lock_nonblocking_tryacquire_conformance(async_client, clean_async):
    await run_lock_nonblocking_tryacquire_conformance(
        lambda: RedisLockStore(async_client, namespace=clean_async)
    )


# ── dead_letter (sync) ────────────────────────────────────────────────────────


async def test_redis_dead_letter_conformance(sync_client, clean_sync):
    await run_dead_letter_conformance(
        lambda: RedisDeadLetterQueue(sync_client, namespace=clean_sync, max_retries=3)
    )


async def test_redis_dead_letter_escalation_conformance(sync_client, clean_sync):
    await run_dead_letter_escalation_conformance(
        lambda: RedisDeadLetterQueue(sync_client, namespace=clean_sync, max_retries=3)
    )


async def test_redis_dead_letter_message_isolation_conformance(sync_client, clean_sync):
    await run_dead_letter_message_isolation_conformance(
        lambda: RedisDeadLetterQueue(sync_client, namespace=clean_sync, max_retries=3)
    )
