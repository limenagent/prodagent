"""Self-wiring capability bundles — lazy surface (kernel-friendly)."""

from __future__ import annotations

from prodagent.base.lazy import lazy_package

_SYMBOL_SOURCES: dict[str, str] = {
    "MemoryHooks": "prodagent.hooks.bundles.memory",
    "SpanObserverHooks": "prodagent.hooks.bundles.observability",
    "ConsoleObserverHooks": "prodagent.hooks.observers.console",
    "LearningHooks": "prodagent.hooks.bundles.learning",
}

__all__ = sorted(_SYMBOL_SOURCES)

__getattr__, __dir__ = lazy_package(_SYMBOL_SOURCES)
