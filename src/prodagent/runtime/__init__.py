"""prodagent.runtime — agent assembly, execution strategies, coordination."""

from __future__ import annotations

from typing import Any

__all__ = ["Agent", "AgentConfig", "Workflow", "drive", "drive_stream"]

_SYMBOL_SOURCES: dict[str, str] = {
    "Agent": "prodagent.runtime.agent",
    "AgentConfig": "prodagent.runtime.config",
    "Workflow": "prodagent.runtime.workflow",
    "drive": "prodagent.runtime.runner",
    "drive_stream": "prodagent.runtime.runner",
}


def __getattr__(name: str) -> Any:
    source = _SYMBOL_SOURCES.get(name)
    if source is None:
        raise AttributeError(f"module 'prodagent.runtime' has no attribute {name!r}")
    import importlib

    module = importlib.import_module(source)
    value = getattr(module, name)
    globals()[name] = value
    return value


def __dir__() -> list[str]:
    return sorted(__all__)
