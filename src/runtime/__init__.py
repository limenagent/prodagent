"""src.runtime —— 用内核原语拼出来的策略/配方层。

kernel 只提供机制；这里的每一样东西都是“怎么用这些机制”的一种配方，都可以
被你自己的实现替换：ReAct、plan-first、多 Agent 协作、上下文管理、长期记忆、
技能、MCP 工具桥，全部不进内核。

对使用者更友好的门面（Agent / Workflow）在 agent.py、workflow.py。
"""

from src.runtime.agent import Agent, AgentResult
from src.runtime.workflow import (
    Workflow,
    WorkflowResult,
    fork,
    go,
    hand_off,
    wait_human,
)

__all__ = [
    "Agent",
    "AgentResult",
    "Workflow",
    "WorkflowResult",
    "fork",
    "go",
    "hand_off",
    "wait_human",
]
