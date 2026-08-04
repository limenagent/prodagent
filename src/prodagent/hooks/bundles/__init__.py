"""Self-wiring capability bundles."""

from prodagent.hooks.bundles.base import HookBundle, default_hook_bundles
from prodagent.hooks.bundles.learning import LearningHooks
from prodagent.hooks.bundles.memory import MemoryHooks
from prodagent.hooks.bundles.observability import SpanObserverHooks
from prodagent.hooks.observers.console import ConsoleObserverHooks

__all__ = [
    "HookBundle",
    "default_hook_bundles",
    "MemoryHooks",
    "SpanObserverHooks",
    "ConsoleObserverHooks",
    "LearningHooks",
]
