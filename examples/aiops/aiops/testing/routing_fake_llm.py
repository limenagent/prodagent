"""按 system prompt 分发到 per-agent 脚本的 routing FakeLLM。

aiops 新架构里，investigator 并行 spawn 3 个诊断子 agent，外加一个
remediator。子 agent 共享父 LLM，但每个需要不同的响应（log_analysis 调
tail_logs，deploy 调 get_recent_deploys 等）。单一响应队列搞不定，因为
并发子 agent 调用会以非确定顺序 pop 响应。

routing LLM 嗅探 system prompt 里的 ``# {name} Agent``，分发到该 agent 的
per-call 队列。每个 agent 的队列独立，并发安全。
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from prodagent import LLMClient, LLMConfig
from prodagent.core.types import LLMResponse, MessageList

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable


class _AgentQueue:
    """per-agent 的 LLMResponse FIFO 队列，带 echo 回退。"""

    def __init__(self, responses: list[LLMResponse] | None = None) -> None:
        self._responses = list(responses or [])
        self._calls = 0

    async def complete(
        self,
        messages: MessageList,
        *,
        on_chunk: Callable[[str], Awaitable[None]] | None = None,
    ) -> LLMResponse:
        self._calls += 1
        if self._responses:
            return self._responses.pop(0)
        last_user = next(
            (m["content"] for m in reversed(messages) if m.get("role") == "user"),
            "(no user message)",
        )
        return LLMResponse(
            content=f"[routing-llm fallback] {last_user}",
            stop_reason="end_turn",
            input_tokens=50,
            output_tokens=10,
        )


class RoutingFakeLLM(LLMClient):
    """按 system prompt 把 complete() 分发到 per-agent 队列。

    用 ``add(name, responses)`` 把 agent 名映射到它的队列。system prompt
    不匹配任何已知 agent 的调用回退到 ``default`` 队列 —— 适合父
    investigator，它的 system prompt 是组装时的 context，不是单个名字。
    """

    def __init__(self) -> None:
        self._queues: dict[str, _AgentQueue] = {}
        self._default = _AgentQueue()
        self._call_count = 0

    @property
    def call_count(self) -> int:
        return self._call_count

    def add(self, agent_name: str, responses: list[LLMResponse]) -> _AgentQueue:
        q = _AgentQueue(responses)
        self._queues[agent_name] = q
        return q

    def set_default(self, responses: list[LLMResponse]) -> _AgentQueue:
        self._default = _AgentQueue(responses)
        return self._default

    def _resolve(self, system: str) -> _AgentQueue:
        # system prompt 形如: "# {name} Agent\n\n## Context\n..."
        for name, q in self._queues.items():
            if f"# {name} Agent" in system:
                return q
        return self._default

    async def complete(
        self,
        messages: MessageList,
        *,
        system: str = "",
        tools: list[dict[str, Any]] | None = None,
        config: LLMConfig | None = None,
        on_chunk: Callable[[str], Awaitable[None]],
    ) -> LLMResponse:
        self._call_count += 1
        q = self._resolve(system)
        return await q.complete(messages, on_chunk=on_chunk)
