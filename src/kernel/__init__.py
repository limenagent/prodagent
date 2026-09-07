"""src.kernel — public surface of the teaching-grade agent kernel."""

from src.kernel.body import (
    FnBody,
    LLMBody,
    NodeBody,
    NodeContext,
    Outcome,
    SubPlanBody,
    ToolBody,
)
from src.kernel.bus import BlockingResult, Bus, Subscription
from src.kernel.channels import (
    AmbiguousWrite,
    Channel,
    WaveWrites,
    add,
    append,
    last,
    merge,
)
from src.kernel.command import Command, Goto, Send
from src.kernel.eventlog import (
    CheckpointStore,
    Event,
    EventLog,
    InMemoryEventLog,
    InMemoryStore,
    apply_event,
    fold_events,
)
from src.kernel.graph import Edge, Node, Plan, RetryPolicy
from src.kernel.ports import LlmPort, LlmReply, SubagentPort, ToolPort
from src.kernel.run import Interrupt, NodeRuntimeState, Run
from src.kernel.scheduler import InProcessActivator, Scheduler
from src.kernel.types import (
    NodeStatus,
    RunState,
    ToolCall,
    ToolResult,
)

__all__ = [
    "AmbiguousWrite",
    "BlockingResult",
    # bus / port types
    "Bus",
    "Channel",
    "CheckpointStore",
    # commands
    "Command",
    "Edge",
    # events / storage
    "Event",
    "EventLog",
    "FnBody",
    "Goto",
    "InMemoryEventLog",
    "InMemoryStore",
    "InProcessActivator",
    "Interrupt",
    "LLMBody",
    "LlmPort",
    "LlmReply",
    "Node",
    # body
    "NodeBody",
    "NodeContext",
    "NodeRuntimeState",
    "NodeStatus",
    "Outcome",
    # graph and state
    "Plan",
    "RetryPolicy",
    # run
    "Run",
    "RunState",
    # engine
    "Scheduler",
    "Send",
    "SubPlanBody",
    "SubagentPort",
    "Subscription",
    "ToolBody",
    "ToolCall",
    "ToolPort",
    "ToolResult",
    "WaveWrites",
    "add",
    "append",
    "apply_event",
    "fold_events",
    "last",
    "merge",
]
