"""LeafExecutor port — unified contract for the two leaf execution strategies."""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol, runtime_checkable

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator

    from prodagent.kernel.events import AgentEvent


@runtime_checkable
class LeafExecutor(Protocol):
    """Unified contract for the two leaf execution strategies."""

    def stream(
        self,
        task: str,
        *,
        run_id: str | None = None,
        parent_run_id: str | None = None,
    ) -> AsyncGenerator[AgentEvent, None]:
        """Final ``RunCompletedEvent`` carries the run."""
        ...
