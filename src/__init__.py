"""src —— 教学型 Agent 内核。

阅读顺序就是构造顺序（对应专栏模块）：

    types → command → channels          不可变的值与状态合并规则
    graph                               静态蓝图：节点 / 边 / Plan
    body                                唯一可组合接口与四种内置 body
    run                                 一次动态执行：状态机 / 挂起 / 快照
    eventlog                            事件是唯一事实源，状态是折叠投影
    bus / ports                         对外的观察、裁决、注入与可换端口
    scheduler                           波次引擎：把上面所有部件装成一台机器

内核不 import 任何模型厂商或具体工具实现，这一点有测试守住。
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

# —— 好用的门面层（机制在内、好用在外）——
from src.runtime.agent import Agent, AgentResult
from src.runtime.workflow import (
    Workflow,
    WorkflowResult,
    go,
    send,
    wait_human,
)

__all__ = [
    # 门面
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
