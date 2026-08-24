"""Agent lifecycle hook system — event-driven, composable.

The bus itself lives in :mod:`prodagent.kernel.bus`; this package keeps the
observers, bundles, and the HITL approval machinery that plug into it.
"""

from __future__ import annotations

from prodagent.kernel.bus import (  # noqa: F401
    BlockingResult,
    FailurePolicy,
    Gate,
    HookEvent,
    HookRegistry,
    InjectionPoint,
    fire,
    fire_checkpoint_failed,
    save_and_fire_checkpoint,
)

__all__ = [
    "HookEvent",
    "HookRegistry",
    "fire",
    "fire_checkpoint_failed",
    "save_and_fire_checkpoint",
    "BlockingResult",
    "Gate",
    "InjectionPoint",
    "FailurePolicy",
]
