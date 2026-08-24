"""Shim — the registry moved to :mod:`prodagent.kernel.bus`."""

from prodagent.kernel.bus import (  # noqa: F401
    BlockingResult,
    CheckHandler,
    EventHandler,
    FailurePolicy,
    HookEvent,
    HookRegistry,
    InjectorHandler,
)

__all__ = [
    "HookRegistry",
    "HookEvent",
    "BlockingResult",
    "FailurePolicy",
    "EventHandler",
    "CheckHandler",
    "InjectorHandler",
]

