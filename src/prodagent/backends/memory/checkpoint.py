"""In-process checkpoint store — the bare-profile default.

Keeps PLAN_FIRST execution working with zero disk footprint: state dies with
the process; cross-restart durability is the production profile's
``FileCheckpointStore`` (or Postgres).
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

from prodagent.base.errors import VersionConflict

if TYPE_CHECKING:
    from prodagent.kernel.run import Run

__all__ = ["InMemoryCheckpointStore"]


class InMemoryCheckpointStore:
    """Latest-snapshot checkpoint store in process memory.

    Mirrors :class:`~prodagent.backends.file.checkpoint.FileCheckpointStore`
    BASE semantics: idempotent save under ``run_id``, optimistic version
    check, monotonic version bump on every save. EXTENDED capabilities
    (``fork``/``list_versions``) are not implemented — nothing in the
    codebase calls them outside of tests exercising ``FileCheckpointStore``
    directly.
    """

    def __init__(self) -> None:
        self._runs: dict[str, Run] = {}
        self._versions: dict[str, int] = {}
        self._lock = asyncio.Lock()

    async def save(self, run: Run, expected_version: int | None = None) -> None:
        async with self._lock:
            stored_version = self._versions.get(run.run_id, 0)
            if expected_version is not None and expected_version != stored_version:
                raise VersionConflict(
                    f"checkpoint version mismatch for run={run.run_id}: "
                    f"expected {expected_version}, stored {stored_version}."
                )
            run.checkpoint_version = stored_version + 1
            self._runs[run.run_id] = run
            self._versions[run.run_id] = run.checkpoint_version

    async def load(self, run_id: str, version: int | None = None) -> Run | None:
        async with self._lock:
            return self._runs.get(run_id)

    async def list_run_ids(self) -> list[str]:
        async with self._lock:
            return sorted(self._runs)

    async def fork(
        self,
        run_id: str,
        at_version: int,
        new_run_id: str | None = None,
    ) -> str:
        raise NotImplementedError("InMemoryCheckpointStore does not keep version history")

    async def list_versions(self, run_id: str) -> list[int]:
        raise NotImplementedError("InMemoryCheckpointStore does not keep version history")
