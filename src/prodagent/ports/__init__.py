"""Framework-level ports — durable contracts for swappable backends.

Every Protocol the framework exposes lives here. Implementations live under
``prodagent.backends``.
"""

from prodagent.ports.approval import ApprovalStore
from prodagent.ports.cache import CacheStore
from prodagent.ports.checkpoint import CheckpointStore
from prodagent.ports.dead_letter import DeadLetterStore
from prodagent.ports.document import DocumentStore
from prodagent.ports.event_log import EventLog
from prodagent.ports.experience import ExperienceStore
from prodagent.ports.graph import GraphStore
from prodagent.ports.leaf_executor import LeafExecutor
from prodagent.ports.llm import LLMClient
from prodagent.ports.lock import LockStore, LockToken
from prodagent.ports.session import SessionStore
from prodagent.ports.span import SpanExporter
from prodagent.ports.tool import Tool
from prodagent.ports.vector import VectorHit, VectorStore

__all__ = [
    "ApprovalStore",
    "CacheStore",
    "CheckpointStore",
    "DeadLetterStore",
    "DocumentStore",
    "EventLog",
    "ExperienceStore",
    "GraphStore",
    "LeafExecutor",
    "LLMClient",
    "LockStore",
    "LockToken",
    "SessionStore",
    "SpanExporter",
    "Tool",
    "VectorHit",
    "VectorStore",
]
