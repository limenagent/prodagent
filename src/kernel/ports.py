"""ports — the boundary between the kernel and the outside world (DIP).

The kernel only knows these Protocols. It does not know OpenAI, does not know
a vector store, and does not know whether your tool is an HTTP call or a local
function. The composition root (startup) injects concrete implementations; in
tests we swap in scripted Fakes, so the whole pipeline runs offline and
deterministically.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

from src.kernel.types import ToolCall, ToolResult


@dataclass(frozen=True)
class LlmReply:
    """Normalized result of one model call: text + tool calls + token count."""

    text: str = ""
    tool_calls: list[ToolCall] = field(default_factory=list)
    tokens: int = 0


@runtime_checkable
class LlmPort(Protocol):
    """Model port: messages in, normalized reply out. Vendors live in adapters.

    ``on_delta`` is an optional streaming callback: the implementation invokes
    it with text fragments as they are produced (e.g. token-by-token UI), and
    still returns the full LlmReply at the end. Callers that don't stream
    simply omit it.
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
    """Tool port: one governed tool call (validation/auth/execution on the impl).

    ``ctx`` is an optional node context so a tool can activate a sub-agent
    (agent-as-tool) when needed; simple implementations that don't care just
    ignore it.
    """

    async def dispatch(self, call: ToolCall, ctx: Any = None) -> ToolResult: ...


@runtime_checkable
class SubagentPort(Protocol):
    """Sub-agent activation port: recursively run a child Run with the same
    kernel (call semantics — it returns a result). node_id lets the activator
    attribute the delegation fact to the spawning node."""

    async def activate(
        self, spec: Any, task: str, parent_run: Any, payload: Any = None, node_id: str = ""
    ) -> dict: ...
