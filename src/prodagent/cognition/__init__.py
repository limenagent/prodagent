"""Context window construction + cross-run memory — lazy surface."""

from __future__ import annotations

from prodagent.base.lazy import lazy_package

_SYMBOL_SOURCES: dict[str, str] = {
    "CompressionLevel": "prodagent.cognition.context.budget",
    "ContextBudget": "prodagent.cognition.context.budget",
    "TokenCounter": "prodagent.cognition.context.budget",
    "HistoryCompressor": "prodagent.cognition.context.compression",
    "Summariser": "prodagent.cognition.context.compression",
    "ContextManager": "prodagent.cognition.context.manager",
    "reduce_on_append": "prodagent.cognition.context.tool_results",
    "MemoryClassifier": "prodagent.cognition.memory.classification",
    "MemoryManager": "prodagent.cognition.memory.manager",
    "build_memory_manager": "prodagent.cognition.memory.manager",
    "MemoryRecord": "prodagent.cognition.memory.storage",
    "MemoryType": "prodagent.cognition.memory.storage",
    "StoredMemory": "prodagent.cognition.memory.storage",
    "DocumentStore": "prodagent.ports.persistence",
    "GraphStore": "prodagent.ports.persistence",
}

__all__ = sorted(_SYMBOL_SOURCES)

__getattr__, __dir__ = lazy_package(_SYMBOL_SOURCES)
