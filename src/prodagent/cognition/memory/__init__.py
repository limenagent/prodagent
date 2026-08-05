"""Cross-run recall / classification / conflict resolution."""

from prodagent.cognition.memory.channels import (
    DEFAULT_MERGE_ORDER,
    EntityChannel,
    ExactChannel,
    RecallContext,
    RecalledItem,
    RuleChannel,
    SemanticChannel,
)
from prodagent.cognition.memory.classification import MemoryClassifier, reasoning_texts
from prodagent.cognition.memory.conflict import (
    ConflictVerdict,
    DefaultConflictPolicy,
    EmbeddingCandidateFilter,
    SupersedeAction,
)
from prodagent.cognition.memory.embedder import HashEmbedder, cosine
from prodagent.cognition.memory.forgetting import RECALL_FLOOR, activation
from prodagent.cognition.memory.manager import MemoryManager, MemoryProvider, build_memory_manager
from prodagent.cognition.memory.storage import (
    MemoryRecord,
    MemoryType,
    StoredMemory,
)
from prodagent.cognition.memory.touch_worker import TouchBackWorker
from prodagent.ports.document import DocumentStore
from prodagent.ports.graph import GraphStore

__all__ = [
    "HashEmbedder",
    "cosine",
    "DocumentStore",
    "GraphStore",
    "MemoryRecord",
    "StoredMemory",
    "MemoryType",
    "RecalledItem",
    "RecallContext",
    "RuleChannel",
    "ExactChannel",
    "SemanticChannel",
    "EntityChannel",
    "DEFAULT_MERGE_ORDER",
    "MemoryClassifier",
    "reasoning_texts",
    "activation",
    "RECALL_FLOOR",
    "EmbeddingCandidateFilter",
    "DefaultConflictPolicy",
    "ConflictVerdict",
    "SupersedeAction",
    "TouchBackWorker",
    "MemoryManager",
    "MemoryProvider",
    "build_memory_manager",
]
