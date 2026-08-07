"""Default bundle adapters — wire the framework's stock hooks onto an Agent."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from prodagent.core.config import FrameworkConfig
    from prodagent.hooks.registry import HookRegistry
    from prodagent.runtime.agent import Agent

logger = logging.getLogger(__name__)


class ConsoleDefaultBundle:
    """User-facing terminal observer — always on."""

    def attach(self, agent: Agent, fw: FrameworkConfig | None, registry: HookRegistry) -> None:
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


class PermissionDefaultBundle:
    """Taint monitoring — dedupes against ``extensions=[PermissionHooks(...)]``."""

    def attach(self, agent: Agent, fw: FrameworkConfig | None, registry: HookRegistry) -> None:
        from prodagent.guardrail.permission import ContextTaintMonitor
        from prodagent.hooks.bundles.security import PermissionHooks

        for ext in agent.config.extensions:
            if isinstance(ext, PermissionHooks):
                return
        monitor = ContextTaintMonitor(tool_registry=agent.tool_registry)
        PermissionHooks(taint_monitor=monitor, tool_registry=agent.tool_registry).attach(registry)


class InjectionDefaultBundle:
    """L1-L5 prompt-injection + PII defense — dedupes against ``extensions=``."""

    def attach(self, agent: Agent, fw: FrameworkConfig | None, registry: HookRegistry) -> None:
        from prodagent.guardrail.injection.pipeline import GuardrailPipeline
        from prodagent.hooks.bundles.security.injection import InjectionDefenseHooks

        for ext in agent.config.extensions:
            if isinstance(ext, InjectionDefenseHooks):
                return
        InjectionDefenseHooks(pipeline=GuardrailPipeline()).attach(registry)


class LearningDefaultBundle:
    """Closed-loop skill synthesis — needs fw + a SkillRegistry."""

    def attach(self, agent: Agent, fw: FrameworkConfig | None, registry: HookRegistry) -> None:
        if fw is None or agent.config.skills is None:
            return
        from prodagent.hooks.bundles.learning import LearningHooks

        LearningHooks(registry=agent.config.skills, framework_config=fw).attach(registry)
