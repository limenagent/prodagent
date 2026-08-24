"""Default bundle adapters — wire the framework's stock hooks onto an Agent."""

from __future__ import annotations

import logging
import os
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from prodagent.core.config import FrameworkConfig
    from prodagent.kernel.bus import HookRegistry
    from prodagent.runtime.agent import Agent

logger = logging.getLogger(__name__)


def _console_enabled(fw: FrameworkConfig | None) -> bool:
    """Console output is opt-in: ``FrameworkConfig(console_observer=True)`` or
    ``PRODAGENT_CONSOLE=1`` (the env check also covers the fw=None path)."""
    if fw is not None and fw.console_observer:
        return True
    return os.getenv("PRODAGENT_CONSOLE", "").lower() in ("1", "true", "yes")


class ConsoleDefaultBundle:
    """Coloured terminal observer — opt-in. A library must stay silent on
    stdout by default; the REPL and playground enable it explicitly."""

    def attach(self, agent: Agent, fw: FrameworkConfig | None, registry: HookRegistry) -> None:
        if not _console_enabled(fw):
            return
        from prodagent.hooks.observers.console import ConsoleObserverHooks

        ConsoleObserverHooks().attach(registry)


class CacheMonitorDefaultBundle:
    """Warns when Prompt Cache hit rate stays low — always on, no fw needed."""

    def attach(self, agent: Agent, fw: FrameworkConfig | None, registry: HookRegistry) -> None:
        from prodagent.hooks.observers.cache_monitor import CacheMonitorHooks

        CacheMonitorHooks().attach(registry)


class SpanDefaultBundle:
    """Span/audit exporter — needs a FrameworkConfig to resolve the exporter."""

    def attach(self, agent: Agent, fw: FrameworkConfig | None, registry: HookRegistry) -> None:
        if fw is None:
            return
        from prodagent.hooks.bundles.observability import SpanObserverHooks

        SpanObserverHooks(framework_config=fw).attach(registry)


class ApprovalDefaultBundle:
    """HITL gate for HIGH side-effect tools — dedupes against ``extensions=``."""

    def attach(self, agent: Agent, fw: FrameworkConfig | None, registry: HookRegistry) -> None:
        from prodagent.hooks.bundles.security import ApprovalHooks

        for ext in agent.config.extensions:
            if isinstance(ext, ApprovalHooks):
                return
        approval_hooks = ApprovalHooks()
        approval_hooks.attach(registry)
        agent.config.approval = approval_hooks.approval_gate


class LearningDefaultBundle:
    """Closed-loop skill synthesis — needs fw + a SkillRegistry."""

    def attach(self, agent: Agent, fw: FrameworkConfig | None, registry: HookRegistry) -> None:
        if fw is None or agent.config.skills is None:
            return
        from prodagent.hooks.bundles.learning import LearningHooks

        LearningHooks(registry=agent.config.skills, framework_config=fw).attach(registry)
