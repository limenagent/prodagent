"""ports —— 内核与外部世界之间的端口（第 32 课的依赖倒置）。

内核只认识这些 Protocol，不认识 OpenAI、不认识某个向量库、不认识你的工具
是 HTTP 还是本地函数。组合根（启动处）负责把具体实现“注入”进来，
测试时则换成脚本化的 Fake，于是整条链路可以离线、确定性地跑。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

from src.kernel.types import ToolCall, ToolResult


@dataclass(frozen=True)
class LlmReply:
    """一次模型调用的归一结果：文本 + 模型请求的工具调用 + token 计量。"""

    text: str = ""
    tool_calls: list[ToolCall] = field(default_factory=list)
    tokens: int = 0


@runtime_checkable
class LlmPort(Protocol):
    """模型端口：吃消息，吐归一结果。具体厂商由适配层实现。

    on_delta 是可选的流式回调：实现方边生成边回调文本片段（如 UI 上的“吐字”），
    最终仍返回完整 LlmReply；不需要流式的调用方不传即可。
    """

    async def chat(
        self,
        messages: list[dict],
        *,
        tools: list[dict] | None = None,
        system: str | None = None,
        on_delta: Any = None,
    ) -> LlmReply: ...


@runtime_checkable
class ToolPort(Protocol):
    """工具端口：一次受治理的工具调用（校验/授权/执行都在实现侧）。

    ctx 是可选的节点上下文，需要时工具能借此激活子 Agent（agent-as-tool）；
    不关心上下文的简单实现忽略它即可。
    """

    async def dispatch(self, call: ToolCall, ctx: Any = None) -> ToolResult: ...


@runtime_checkable
class SubagentPort(Protocol):
    """子 Agent 激活端口：用同一个内核递归跑一个子 Run（call 语义，要返回）。"""

    async def activate(
        self, spec: Any, task: str, parent_run: Any, payload: Any = None
    ) -> dict: ...
