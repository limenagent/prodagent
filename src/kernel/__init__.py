"""src.kernel —— 教学型 Agent 内核的公共出口。"""

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
from src.kernel.command import Command, Goto, Handoff, Send
from src.kernel.eventlog import (
    Event,
    EventLog,
    CheckpointStore,
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
    # 图与状态
    "Plan", "Node", "Edge", "Channel", "WaveWrites", "AmbiguousWrite", "RetryPolicy",
    "last", "append", "add", "merge",
    # body
    "NodeBody", "FnBody", "ToolBody", "LLMBody", "SubPlanBody", "Outcome", "NodeContext",
    # 命令
    "Command", "Goto", "Send", "Handoff",
    # 运行
    "Run", "RunState", "NodeStatus", "NodeRuntimeState", "Interrupt",
    # 事件 / 存储
    "Event", "EventLog", "CheckpointStore", "InMemoryEventLog", "InMemoryStore",
    "apply_event", "fold_events",
    # 总线 / 端口类型
    "Bus", "Subscription", "BlockingResult", "ToolCall", "ToolResult",
    "LlmReply", "LlmPort", "ToolPort", "SubagentPort",
    # 引擎
    "Scheduler", "InProcessActivator",
]
