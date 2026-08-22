"""Cross-run recall / classification / conflict resolution — lazy surface."""

from __future__ import annotations

from prodagent.core.lazy import lazy_package

_SYMBOL_SOURCES: dict[str, str] = {
    "DEFAULT_MERGE_ORDER": "prodagent.cognition.memory.channels",
    "EntityChannel": "prodagent.cognition.memory.channels",
    "ExactChannel": "prodagent.cognition.memory.channels",
    "RecallContext": "prodagent.cognition.memory.channels",
    "RecalledItem": "prodagent.cognition.memory.channels",
    "RuleChannel": "prodagent.cognition.memory.channels",
    "SemanticChannel": "prodagent.cognition.memory.channels",
    "MemoryClassifier": "prodagent.cognition.memory.classification",
    "reasoning_texts": "prodagent.cognition.memory.classification",
    "ConflictVerdict": "prodagent.cognition.memory.conflict",
    "DefaultConflictPolicy": "prodagent.cognition.memory.conflict",
    "EmbeddingCandidateFilter": "prodagent.cognition.memory.conflict",
    "SupersedeAction": "prodagent.cognition.memory.conflict",
    "HashEmbedder": "prodagent.cognition.memory.embedder",
    "cosine": "prodagent.cognition.memory.embedder",
    "RECALL_FLOOR": "prodagent.cognition.memory.forgetting",
    "activation": "prodagent.cognition.memory.forgetting",
    "MemoryManager": "prodagent.cognition.memory.manager",
    "MemoryProvider": "prodagent.cognition.memory.manager",
    "build_memory_manager": "prodagent.cognition.memory.manager",
    "MemoryRecord": "prodagent.cognition.memory.storage",
    "MemoryType": "prodagent.cognition.memory.storage",
    "StoredMemory": "prodagent.cognition.memory.storage",
    "TouchBackWorker": "prodagent.cognition.memory.touch_worker",
    "DocumentStore": "prodagent.ports.document",
    "GraphStore": "prodagent.ports.graph",
}

__all__ = sorted(_SYMBOL_SOURCES)

__getattr__, __dir__ = lazy_package(_SYMBOL_SOURCES)
