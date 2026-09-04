"""HookBundle protocol — the bundle shape the assembly root wires."""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol, runtime_checkable

if TYPE_CHECKING:
    from prodagent.base.config import FrameworkConfig
    from prodagent.kernel.bus import HookRegistry
    from prodagent.runtime.agent import Agent


@runtime_checkable
class HookBundle(Protocol):
    """Self-wiring capability bundle."""

    def attach(self, agent: Agent, fw: FrameworkConfig | None, registry: HookRegistry) -> None: ...


def default_hook_bundles(fw: FrameworkConfig | None = None) -> list[HookBundle]:
    """The profile's bundle manifest — what ``attach_default_hooks`` wires.

    The bare profile stays silent: console is opt-in via env/flag — no
    observer, no span export, no approval gate. The production profile
    restores the full stack. (Bundle selection by profile is decided HERE,
    once — feature wiring never branches on profile.)

    Learning is NOT in the manifest, by ruling (2026-09-04): a configured
    ``skills=`` registry means the agent LOADS runbooks via ``get_skill`` —
    it must never WRITE skills as a silent side effect. The closed learning
    loop is opt-in: ``extensions=[LearningHooks(...)]``."""
    from prodagent.hooks.bundles.default_wiring import (
        ApprovalDefaultBundle,
        CacheMonitorDefaultBundle,
        ConsoleDefaultBundle,
        SpanDefaultBundle,
    )

    if fw is None or fw.profile == "bare":
        return [ConsoleDefaultBundle()]
    return [
        ConsoleDefaultBundle(),
        CacheMonitorDefaultBundle(),
        SpanDefaultBundle(),
        ApprovalDefaultBundle(),
    ]
