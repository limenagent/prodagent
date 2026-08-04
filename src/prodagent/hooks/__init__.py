"""Agent lifecycle hook system — event-driven, composable."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from prodagent.core.state.run import AgentRun  # noqa: F401
    from prodagent.hooks.events import HookEvent  # noqa: F401
    from prodagent.hooks.registry import HookRegistry  # noqa: F401
    from prodagent.ports import CheckpointStore  # noqa: F401


async def fire(hooks: HookRegistry | None, event: HookEvent, **payload: object) -> None:
    """Defined before sub-module imports so callers can import during module init."""
    if hooks is not None:
        await hooks.fire(event, **payload)


async def fire_checkpoint_failed(
    hooks: HookRegistry | None, run: AgentRun, *, was_failed: bool
) -> None:
    # Flag is sticky — poll after every save would re-fire. Callers pass pre-save value.
    from prodagent.hooks.events import HookEvent as _HookEvent

    if hooks is not None and not was_failed and run.checkpoint_failed:
        await hooks.fire(
            _HookEvent.CHECKPOINT_FAILED,
            run_id=run.run_id,
            turns=run.turn_count,
        )


async def save_and_fire_checkpoint(
    store: CheckpointStore,
    run: AgentRun,
    hooks: HookRegistry | None,
    *,
    expected_version: int | None = None,
) -> None:
    """Save run to checkpoint store and fire CHECKPOINT_FAILED if the save flipped the flag."""
    was_failed = run.checkpoint_failed
    await store.save(
        run,
        expected_version=expected_version
        if expected_version is not None
        else run.checkpoint_version,
    )
    await fire_checkpoint_failed(hooks, run, was_failed=was_failed)


from prodagent.hooks.checkpoint import (  # noqa: E402
    BlockingResult,
    CheckPoint,
    FailurePolicy,
    InjectionPoint,
)
from prodagent.hooks.events import HookEvent  # noqa: E402
from prodagent.hooks.registry import (  # noqa: E402
    HookRegistry,
)

__all__ = [
    "HookEvent",
    "HookRegistry",
    "fire",
    "fire_checkpoint_failed",
    "BlockingResult",
    "CheckPoint",
    "InjectionPoint",
    "FailurePolicy",
]
