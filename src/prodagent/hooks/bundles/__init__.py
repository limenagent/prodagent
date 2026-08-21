"""Self-wiring capability bundles.

Re-exports are lazy (module ``__getattr__``): importing
``prodagent.hooks.bundles`` must not drag the learning / memory /
observability bundles — and their heavier dependencies — onto the kernel
import chain. See tests/core/test_import_weight.py.
"""

from __future__ import annotations

from typing import Any

from prodagent.hooks.bundles.base import HookBundle, default_hook_bundles

__all__ = [
    "HookBundle",
    "default_hook_bundles",
    "MemoryHooks",
    "SpanObserverHooks",
    "ConsoleObserverHooks",
    "LearningHooks",
]

_SYMBOL_SOURCES: dict[str, str] = {
    "MemoryHooks": "prodagent.hooks.bundles.memory",
    "SpanObserverHooks": "prodagent.hooks.bundles.observability",
    "ConsoleObserverHooks": "prodagent.hooks.observers.console",
    "LearningHooks": "prodagent.hooks.bundles.learning",
}


def __getattr__(name: str) -> Any:
    source = _SYMBOL_SOURCES.get(name)
    if source is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    import importlib

    module = importlib.import_module(source)
    value = getattr(module, name)
    globals()[name] = value
    return value
