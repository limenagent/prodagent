"""Framework-level ports — durable contracts for swappable backends.

Every Protocol the framework exposes lives here. Implementations live under
``prodagent.backends`` — except the message plane's in-process Transport
(implementation beside the plane in ``prodagent.coordination.messaging``)
and the kernel's in-process BudgetLedger (``prodagent.kernel.budget``);
both satisfy their port structurally, like every backend.
"""

from prodagent.ports.approval import ApprovalStore
from prodagent.ports.budget_ledger import BudgetLedgerPort, SpendView
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
from prodagent.ports.transport import Transport

__all__ = [
    "ApprovalStore",
    "BudgetLedgerPort",
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
    "SpendView",
    "SpanExporter",
    "Tool",
    "Transport",
]
