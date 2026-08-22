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


def default_hook_bundles(fw: FrameworkConfig | None = None) -> list[HookBundle]:
    """Ordered list of bundles that ``attach_default_hooks`` wires.

    The bare profile stays silent: console is opt-in via env/flag, learning
    only attaches when ``skills=`` is set — no observer, no span export, no
    approval gate. The production profile restores the full stack.
    """
    from prodagent.hooks.bundles.default_wiring import (
        ApprovalDefaultBundle,
        CacheMonitorDefaultBundle,
        ConsoleDefaultBundle,
        LearningDefaultBundle,
        SpanDefaultBundle,
    )

    if fw is None or fw.profile == "bare":
        return [ConsoleDefaultBundle(), LearningDefaultBundle()]
    return [
        ConsoleDefaultBundle(),
        CacheMonitorDefaultBundle(),
        SpanDefaultBundle(),
        ApprovalDefaultBundle(),
        LearningDefaultBundle(),
    ]
