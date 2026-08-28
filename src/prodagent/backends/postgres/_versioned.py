"""Shared optimistic-concurrency routine for postgres checkpoint/session stores."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from prodagent.base.errors import VersionConflict

if TYPE_CHECKING:
    from psycopg import AsyncCursor


async def lock_and_check_version(
    cur: AsyncCursor[Any],
    lock_key: str,
    version_query: str,
    version_params: tuple[Any, ...],
    expected_version: int | None,
    conflict_subject: str,
) -> int:
    """Acquire the per-row advisory lock and enforce optimistic concurrency.

    The version check alone races under concurrent inserts (two writers both
    read the same current value); the transaction-scoped advisory lock
    serializes writers per subject and frees itself at commit — no lock
    rows, no cleanup on crash.

    Returns the current version so the caller can increment it and write.
    """
    await cur.execute("SELECT pg_advisory_xact_lock(hashtext(%s))", (lock_key,))
    await cur.execute(version_query, version_params)
    row = await cur.fetchone()
    current = int(row[0]) if row else 0
    if expected_version is not None and current != expected_version:
        raise VersionConflict(
            f"expected version {expected_version} for {conflict_subject}, "
            f"found {current} — concurrent writer won"
        )
    return current
