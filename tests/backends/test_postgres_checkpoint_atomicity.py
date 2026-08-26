"""Regression: ``PostgresCheckpointStore.save`` must take the per-run advisory
lock inside the same transaction as the ``MAX(version)`` read + INSERT.

Without the xact lock, two replicas can both read the same tail and the
documented ``VersionConflict`` guarantee is lost (cf. ``PostgresEventLog``).
These tests assert the SQL ordering with a mock pool — no live Postgres needed.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from prodagent.backends.postgres.checkpoint import PostgresCheckpointStore
from prodagent.base.errors import VersionConflict
from prodagent.kernel.state import AgentRun


class _AsyncCM:
    """Minimal async context manager wrapper for mocks."""

    def __init__(self, value: object) -> None:
        self._value = value

    async def __aenter__(self) -> object:
        return self._value

    async def __aexit__(self, *args: object) -> None:
        return None


def _mock_pool() -> tuple[MagicMock, AsyncMock, AsyncMock]:
    pool = MagicMock()
    pool._prodagent_schema_ready = True  # skip DDL in ensure_schema_via_pool_async
    cur = AsyncMock()
    cur.fetchone.return_value = (0,)
    conn = AsyncMock()
    # cursor()/connection() are SYNC calls returning async context managers
    conn.cursor = MagicMock(return_value=_AsyncCM(cur))
    pool.connection = MagicMock(return_value=_AsyncCM(conn))
    return pool, conn, cur


@pytest.mark.asyncio
async def test_save_takes_advisory_lock_before_version_read_and_insert():
    pool, conn, cur = _mock_pool()
    store = PostgresCheckpointStore(pool, namespace="ns")
    run = AgentRun(run_id="r1", task="t")

    await store.save(run, expected_version=0)

    statements = [c.args[0] for c in cur.execute.call_args_list]
    lock_idx = next(i for i, s in enumerate(statements) if "pg_advisory_xact_lock" in s)
    max_idx = next(i for i, s in enumerate(statements) if "MAX(version)" in s)
    insert_idx = next(i for i, s in enumerate(statements) if "INSERT INTO pa_checkpoint" in s)
    assert lock_idx < max_idx < insert_idx
    assert run.checkpoint_version == 1
    conn.commit.assert_awaited()


@pytest.mark.asyncio
async def test_save_raises_version_conflict_on_stale_tail():
    pool, conn, cur = _mock_pool()
    cur.fetchone.return_value = (3,)  # a concurrent writer already bumped the tail
    store = PostgresCheckpointStore(pool, namespace="ns")
    run = AgentRun(run_id="r1", task="t")

    with pytest.raises(VersionConflict):
        await store.save(run, expected_version=0)
    # no INSERT may reach the DB on a conflict
    assert not any("INSERT INTO pa_checkpoint" in c.args[0] for c in cur.execute.call_args_list)
