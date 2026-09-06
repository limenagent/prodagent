"""src.runtime — the strategy/recipe layer assembled from kernel primitives.

The kernel provides only mechanism; everything here is a recipe for *how to use
those mechanisms*, and each piece is replaceable by your own implementation:
ReAct, plan-first, multi-agent collaboration, context management, long-term
memory, skills, and the MCP tool bridge — none of them lives in the kernel.

The friendlier facades for users (Agent / Workflow) live in agent.py and
workflow.py.
"""

from src.runtime.agent import Agent, AgentResult
from src.runtime.workflow import (
    Workflow,
    WorkflowResult,
    go,
    send,
    wait_human,
)

__all__ = [
    "Agent",
    "AgentResult",
    "Workflow",
    "WorkflowResult",
    "go",
    "send",
    "wait_human",
]
