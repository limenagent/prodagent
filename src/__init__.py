"""src — a teaching-grade agent kernel.

The reading order is the construction order (matching the column's modules):

    types → command → channels          immutable values and state merge rules
    graph                               static blueprint: nodes / edges / Plan
    body                                the single composable interface and four built-in bodies
    run                                 one dynamic execution: state machine / suspend / snapshot
    eventlog                            events are the source of truth, state is a folded projection
    bus / ports                         outward observe/adjudicate/inject and replaceable ports
    scheduler                           the wave engine that assembles all the above into a machine

The kernel imports no model vendor or concrete tool implementation; a test
guards this invariant.
"""

from src.kernel import (
    Bus,
    Channel,
    Command,
    EventLog,
    FnBody,
    Goto,
    InMemoryEventLog,
    InMemoryStore,
    Interrupt,
    LLMBody,
    Node,
    NodeBody,
    NodeContext,
    Outcome,
    Plan,
    Run,
    RunState,
    Scheduler,
    Send,
    SubPlanBody,
    ToolBody,
    add,
    append,
    last,
    merge,
)

# ---- Ergonomic facade layer (mechanism inside, ergonomics outside) ----
from src.runtime.agent import Agent, AgentResult
from src.runtime.workflow import (
    Workflow,
    WorkflowResult,
    go,
    send,
    wait_human,
)

# Version single source of truth: pyproject reads it dynamically for releases.
__version__ = "2.0.1"

__all__ = [
    # facade
    "Agent",
    "AgentResult",
    "Bus",
    "Channel",
    "Command",
    "EventLog",
    "FnBody",
    "Goto",
    "InMemoryEventLog",
    "InMemoryStore",
    "Interrupt",
    "LLMBody",
    "Node",
    "NodeBody",
    "NodeContext",
    "Outcome",
    "Plan",
    "Run",
    "RunState",
    "Scheduler",
    "Send",
    "SubPlanBody",
    "ToolBody",
    "Workflow",
    "WorkflowResult",
    "add",
    "append",
    "go",
    "last",
    "merge",
    "send",
    "wait_human",
]
