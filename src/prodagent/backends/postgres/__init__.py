"""Postgres-backed implementations of ``prodagent.ports``.

Distributed + durable across replicas: one Postgres instance serves many
agent processes. State survives process restarts (Postgres WAL) and is
visible to every node. Use when ``FrameworkConfig.backend.durable='postgres'``
or ``ephemeral='postgres'``.

Requires the ``[postgres]`` extra::

    pip install prodagent[postgres]

Schema is auto-created on first use via ``ensure_schema`` — every table is
``CREATE TABLE IF NOT EXISTS``, so it is safe to call on every store init.
All rows carry a ``namespace`` column so multiple agents sharing one DB do
not collide (same isolation contract as the redis backend's key prefix).
"""

from __future__ import annotations

from prodagent.backends.postgres.connection import (
    async_pool_from_env,
    sync_pool_from_env,
)
from prodagent.backends.postgres.schema import (
    ensure_schema,
    ensure_schema_async,
    ensure_schema_via_pool,
    ensure_schema_via_pool_async,
)

__all__ = [
    "async_pool_from_env",
    "sync_pool_from_env",
    "ensure_schema",
    "ensure_schema_async",
    "ensure_schema_via_pool",
    "ensure_schema_via_pool_async",
]
