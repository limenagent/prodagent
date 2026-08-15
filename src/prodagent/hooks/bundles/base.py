"""HookBundle protocol + default bundle list."""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol, runtime_checkable

if TYPE_CHECKING:
    from prodagent.core.config import FrameworkConfig
    from prodagent.hooks.registry import HookRegistry
    from prodagent.runtime.agent import Agent


@runtime_checkable
class HookBundle(Protocol):
    """Self-wiring capability bundle."""

    def attach(self, agent: Agent, fw: FrameworkConfig | None, registry: HookRegistry) -> None: ...


def default_hook_bundles() -> list[HookBundle]:
    """Ordered list of bundles that ``attach_default_hooks`` wires."""
    from prodagent.hooks.bundles.default_wiring import (
        ApprovalDefaultBundle,
        CacheMonitorDefaultBundle,
        ConsoleDefaultBundle,
        LearningDefaultBundle,
        SpanDefaultBundle,
    )

    return [
        ConsoleDefaultBundle(),
        CacheMonitorDefaultBundle(),
        SpanDefaultBundle(),
        ApprovalDefaultBundle(),
        LearningDefaultBundle(),
    ]
