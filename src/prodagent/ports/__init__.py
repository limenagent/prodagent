"""Framework-level ports — durable contracts for swappable backends.

Every Protocol the framework exposes lives here, grouped into family
modules (execution / persistence / observability — the book's
ch1 §1.3.2 five families, plus the single-socket files llm / tool /
budget_ledger and the agent_events wire model). Implementations live under
``prodagent.backends``
and the kernel's in-process BudgetLedger (``prodagent.kernel.budget``);
both satisfy their port structurally, like every backend.
"""

from prodagent.base.determinism import (
    IdPort,
    RandomPort,
    SystemIds,
    SystemRandomness,
    SystemTime,
    TimePort,
)
from prodagent.ports.agent_events import (
    AgentEvent,
    RunCompletedEvent,
    RunFailedEvent,
    RunSuspendedEvent,
    event_from_wire,
    event_to_wire,
)
from prodagent.ports.budget_ledger import BudgetLedgerPort, SpendView
from prodagent.ports.execution import (
    AgentActivation,
    AgentSpec,
    Executor,
    HandoffActivation,
    InProcessChatRunner,
    RunnerPort,
)
from prodagent.ports.llm import LLMClient
from prodagent.ports.persistence import LockStore, LockToken
from prodagent.ports.observability import (
    ApprovalStore,
    CacheStore,
    EventLog,
    SpanExporter,
)
from prodagent.ports.persistence import (
    CheckpointStore,
    DocumentStore,
    ExperienceStore,
    GraphStore,
    SessionStore,
)
from prodagent.ports.tool import Tool

__all__ = [
    "AgentEvent",
    "AgentSpec",
    "AgentActivation",
    "ApprovalStore",
    "BudgetLedgerPort",
    "CacheStore",
    "CheckpointStore",
    "DocumentStore",
    "IdPort",
    "RandomPort",
    "SystemIds",
    "SystemRandomness",
    "SystemTime",
    "TimePort",
    "EventLog",
    "ExperienceStore",
    "GraphStore",
    "RunCompletedEvent",
    "RunFailedEvent",
    "RunSuspendedEvent",
    "event_from_wire",
    "event_to_wire",
    "HandoffActivation",
    "InProcessChatRunner",
    "Executor",
    "LLMClient",
    "LockStore",
    "LockToken",
    "RunnerPort",
    "SessionStore",
    "SpendView",
    "SpanExporter",
    "Tool",
]
