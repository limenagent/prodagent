"""prodagent.runtime — the Agent and the REACTIVE leaf executor."""

from __future__ import annotations

from prodagent.base.lazy import lazy_package

_SYMBOL_SOURCES: dict[str, str] = {
    "Agent": "prodagent.runtime.agent",
    "AgentConfig": "prodagent.runtime.config",
    "drive": "prodagent.runtime.runner",
    "drive_stream": "prodagent.runtime.runner",
}

__all__ = sorted(_SYMBOL_SOURCES)

__getattr__, __dir__ = lazy_package(_SYMBOL_SOURCES)
