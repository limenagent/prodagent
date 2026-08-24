"""Checkpoint port — durable snapshot path for save and resume a run."""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol, runtime_checkable

if TYPE_CHECKING:
    from prodagent.kernel.state import AgentRun


@runtime_checkable
class CheckpointStore(Protocol):
    """Durable snapshot path — save and resume a run.

    Capabilities:
      BASE (required): save, load, list_run_ids
      EXTENDED (optional): fork, list_versions

    Implementations that lack EXTENDED capabilities must still accept calls
    to ``save`` with ``expected_version`` for optimistic concurrency.
    """

    async def save(self, run: AgentRun, expected_version: int | None = None) -> None:
        """Idempotent atomic persist under ``run.run_id``.

        ``expected_version`` enables optimistic concurrency: raise
        ``VersionConflict`` if the stored version differs.
        """
        ...

    async def load(self, run_id: str, version: int | None = None) -> AgentRun | None:
        """Return the ``AgentRun`` for ``run_id``, or ``None`` if absent.

        ``version=None`` means latest; stores without version history may
        ignore it.
        """
        ...

    async def list_run_ids(self) -> list[str]:
        """All run ids with at least one checkpoint."""
        ...

    # --- EXTENDED capabilities (optional) -------------------------------

    async def fork(
        self,
        run_id: str,
        at_version: int,
        new_run_id: str | None = None,
    ) -> str:
        """Create a new run from a historical snapshot, return its id.

        Implementations may raise ``NotImplementedError`` if they do not
        keep version history.
        """
        ...

    async def list_versions(self, run_id: str) -> list[int]:
        """Versions available for ``run_id``, ascending. Empty if none."""
        ...
