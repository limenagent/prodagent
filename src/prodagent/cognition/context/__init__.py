"""Context window construction — lazy surface, kernel-friendly."""

from __future__ import annotations

from prodagent.core.lazy import lazy_package

_SYMBOL_SOURCES: dict[str, str] = {
    "CompressionLevel": "prodagent.cognition.context.budget",
    "ContextBudget": "prodagent.cognition.context.budget",
    "Layer": "prodagent.cognition.context.budget",
    "TokenCounter": "prodagent.cognition.context.budget",
    "HistoryCompressor": "prodagent.cognition.context.compression",
    "Stage": "prodagent.cognition.context.compression",
    "Summariser": "prodagent.cognition.context.compression",
    "ContextManager": "prodagent.cognition.context.manager",
    "ToolResultSpillStore": "prodagent.cognition.context.spill",
    "reduce_on_append": "prodagent.cognition.context.tool_results",
}

__all__ = sorted(_SYMBOL_SOURCES)

__getattr__, __dir__ = lazy_package(_SYMBOL_SOURCES)
