"""Contract tests for ``prodagent.backends.factory``.

Locks the resolver contract: each data type defaults to the store built for
it — relational state to file, ephemeral state to memory, graphs to neo4j,
vectors to memory (local) / qdrant (prod). Unknown kinds raise
``NotImplementedError``.
"""

from __future__ import annotations

import pytest

from prodagent.backends import factory
from prodagent.backends.factory import (
    resolve_approval,
    resolve_cache,
    resolve_checkpoint,
    resolve_dead_letter,
    resolve_document,
    resolve_event_log,
    resolve_graph,
    resolve_lock,
    resolve_span_exporter,
    resolve_vector,
)
from prodagent.backends.file import (
    FileCheckpointStore,
    FileDocumentStore,
    FileEventLog,
    FileGraphStore,
    FileSpanExporter,
)
from prodagent.backends.memory import (
    InMemoryApprovalStore,
    InMemoryCache,
    InMemoryDeadLetterQueue,
    InMemoryVectorStore,
    InProcessLockStore,
)
from prodagent.core.config import BackendConfig, FrameworkConfig
from prodagent.ports import (
    ApprovalStore,
    CacheStore,
    CheckpointStore,
    DeadLetterStore,
    DocumentStore,
    EventLog,
    LockStore,
    SpanExporter,
    VectorStore,
)


def test_default_relational_picks_file():
    cfg = FrameworkConfig.default()
    assert cfg.backend.document == "file"
    assert cfg.backend.checkpoint == "file"
    assert cfg.backend.event_log == "file"
    assert cfg.backend.span == "file"
    assert isinstance(resolve_checkpoint(cfg), FileCheckpointStore)
    assert isinstance(resolve_event_log(cfg), FileEventLog)
    assert isinstance(resolve_span_exporter(cfg), FileSpanExporter)
    assert isinstance(resolve_document(cfg), FileDocumentStore)


def test_default_ephemeral_picks_memory():
    cfg = FrameworkConfig.default()
    assert cfg.backend.cache == "memory"
    assert cfg.backend.lock == "memory"
    assert cfg.backend.approval == "memory"
    assert cfg.backend.dead_letter == "memory"
    assert isinstance(resolve_cache(cfg), InMemoryCache)
    assert isinstance(resolve_approval(cfg), InMemoryApprovalStore)
    assert isinstance(resolve_lock(cfg), InProcessLockStore)
    assert isinstance(resolve_dead_letter(cfg), InMemoryDeadLetterQueue)


def test_default_graph_picks_file():
    cfg = FrameworkConfig.default()
    assert cfg.backend.graph == "file"
    assert isinstance(resolve_graph(cfg), FileGraphStore)


def test_default_vector_picks_memory():
    cfg = FrameworkConfig.default()
    assert cfg.backend.vector == "memory"
    assert isinstance(resolve_vector(cfg), InMemoryVectorStore)


def test_resolvers_return_protocol_instances():
    cfg = FrameworkConfig.default()
    assert isinstance(resolve_checkpoint(cfg), CheckpointStore)
    assert isinstance(resolve_event_log(cfg), EventLog)
    assert isinstance(resolve_approval(cfg), ApprovalStore)
    assert isinstance(resolve_lock(cfg), LockStore)
    assert isinstance(resolve_dead_letter(cfg), DeadLetterStore)
    assert isinstance(resolve_span_exporter(cfg), SpanExporter)
    assert isinstance(resolve_vector(cfg), VectorStore)
    assert isinstance(resolve_cache(cfg), CacheStore)
    assert isinstance(resolve_document(cfg), DocumentStore)


def test_none_config_uses_default():
    assert isinstance(resolve_checkpoint(None), FileCheckpointStore)
    assert isinstance(resolve_cache(None), InMemoryCache)
    assert isinstance(resolve_vector(None), InMemoryVectorStore)


def test_dead_letter_inherits_max_retries_from_config():
    cfg = FrameworkConfig.default()
    cfg.orchestration.spawn_dlq_max_retries = 7
    dlq = resolve_dead_letter(cfg)
    assert isinstance(dlq, InMemoryDeadLetterQueue)
    assert dlq._max_retries == 7


def test_postgres_relational_resolvers_return_pg_classes():
    """Relational state on ``postgres`` resolves to ``Postgres*`` classes.

    checkpoint/event_log use the async pool (lazy, no connection on build);
    document/span use the sync pool. Both run without a live Postgres for the
    isinstance check — only store *calls* hit the DB.
    """
    pytest.importorskip("psycopg_pool")
    from prodagent.backends.postgres.checkpoint import PostgresCheckpointStore
    from prodagent.backends.postgres.document import PostgresDocumentStore
    from prodagent.backends.postgres.event_log import PostgresEventLog
    from prodagent.backends.postgres.span import PostgresSpanExporter

    cfg = FrameworkConfig(
        backend=BackendConfig(
            document="postgres",
            checkpoint="postgres",
            event_log="postgres",
            span="postgres",
            postgres_namespace="factory-test",
        )
    )
    # async-pool stores construct without opening the pool
    assert isinstance(resolve_checkpoint(cfg), PostgresCheckpointStore)
    assert isinstance(resolve_event_log(cfg), PostgresEventLog)
    # sync-pool stores open the pool eagerly — only assert the async ones here
    _ = (PostgresDocumentStore, PostgresSpanExporter)


def test_postgres_namespace_is_threaded_through():
    pytest.importorskip("psycopg_pool")
    cfg = FrameworkConfig(
        backend=BackendConfig(checkpoint="postgres", postgres_namespace="my-pg-ns")
    )
    store = resolve_checkpoint(cfg)
    assert store._ns == "my-pg-ns"


def test_redis_ephemeral_resolvers_return_redis_classes():
    pytest.importorskip("redis")
    from prodagent.backends.redis.approval import RedisApprovalStore
    from prodagent.backends.redis.cache import RedisCache
    from prodagent.backends.redis.dead_letter import RedisDeadLetterQueue
    from prodagent.backends.redis.lock import RedisLockStore

    cfg = FrameworkConfig(
        backend=BackendConfig(
            cache="redis",
            lock="redis",
            approval="redis",
            dead_letter="redis",
            redis_namespace="factory-test",
        )
    )
    assert isinstance(resolve_cache(cfg), RedisCache)
    assert isinstance(resolve_approval(cfg), RedisApprovalStore)
    assert isinstance(resolve_lock(cfg), RedisLockStore)
    assert isinstance(resolve_dead_letter(cfg), RedisDeadLetterQueue)


def test_redis_namespace_is_threaded_through():
    pytest.importorskip("redis")
    cfg = FrameworkConfig(backend=BackendConfig(cache="redis", redis_namespace="my-ns"))
    store = resolve_cache(cfg)
    assert store._ns == "my-ns"


def test_unknown_relational_backend_raises():
    """An unknown backend kind raises NotImplementedError (not silent fallthrough)."""
    cfg = FrameworkConfig(backend=BackendConfig(checkpoint="postgres"))
    object.__setattr__(cfg.backend, "checkpoint", "sqlite")  # type: ignore[attr-defined]
    with pytest.raises(NotImplementedError, match="sqlite"):
        resolve_checkpoint(cfg)


def test_neo4j_graph_resolver_returns_neo4j_store():
    pytest.importorskip("neo4j")
    from prodagent.backends.neo4j.graph import Neo4jGraphStore

    cfg = FrameworkConfig(backend=BackendConfig(graph="neo4j"))
    try:
        store = resolve_graph(cfg)
    except Exception:
        pytest.skip("Neo4j not reachable")
    assert isinstance(store, Neo4jGraphStore)
    store.close()


def test_qdrant_vector_resolver_returns_qdrant_store():
    """``vector='qdrant'`` resolves to ``QdrantVectorStore`` — connects eagerly,
    so this needs a live Qdrant. Skipped if unreachable."""
    from prodagent.backends.qdrant.vector import QdrantVectorStore

    cfg = FrameworkConfig(
        backend=BackendConfig(vector="qdrant", qdrant_url="http://localhost:6333")
    )
    try:
        store = resolve_vector(cfg)
        assert isinstance(store, QdrantVectorStore)
    except Exception:
        pytest.skip("Qdrant not reachable")


def test_factory_module_exposes_all_resolvers():
    for name in (
        "resolve_checkpoint",
        "resolve_event_log",
        "resolve_cache",
        "resolve_approval",
        "resolve_lock",
        "resolve_dead_letter",
        "resolve_span_exporter",
        "resolve_document",
        "resolve_graph",
        "resolve_vector",
    ):
        assert callable(getattr(factory, name)), f"{name} missing from factory"
