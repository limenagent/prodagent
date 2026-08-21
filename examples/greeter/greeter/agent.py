"""Greeter —— 最小的端到端 ProdAgent 示例。

本示例展示:
  - ``Agent`` + ``@tool`` + ``mode="reactive"`` 是能跑的 agent 的最小骨架。
    没有 hooks、没有记忆、没有预算、没有 checkpoint。
  - ``greet`` 是 readonly LOW 副作用工具 —— 最安全的层级。
  - 没传 hooks 时框架自动挂载 ``ConsoleObserverHooks``,终端免费看到
    完整生命周期。

保持这个 example 极小: 它是"ProdAgent 最小程序长什么样"的参考答案。
后面的 example 在这个骨架上加能力。
"""

from __future__ import annotations

from prodagent import Agent, AgentConfig, ExecutionMode, FrameworkConfig, tool


@tool(name="greet", readonly=True)
async def greet(name: str) -> str:
    """按名字打招呼。

    Args:
        name: 要打招呼的人。
    """
    return f"Hello, {name}! Welcome to ProdAgent."


DEFAULT_TASK = "跟 Alice 打个招呼。"


def build_greeter_agent(*, framework_config: FrameworkConfig | None = None) -> Agent:
    return Agent(
        "greeter",
        system_prompt="你是友好的 greeter。用 greet 工具按名字跟用户打招呼。",
        tools=[greet],
        mode=ExecutionMode.REACTIVE,
        config=AgentConfig(name="greeter", framework=framework_config),
    )
