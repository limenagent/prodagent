"""HookBundle protocol — the bundle shape the assembly root wires."""

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
    """Delegate — the manifest itself lives in the assembly root."""
    from prodagent.runtime.compose import default_bundles

    return default_bundles(fw)
