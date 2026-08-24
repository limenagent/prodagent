from __future__ import annotations

from typing import TYPE_CHECKING, Any

import pytest

from prodagent import Agent, AgentConfig, ExecutionMode
from prodagent.coordination.spawn import build_spawn_tools_for_agent
from prodagent.kernel.types import LLMResponse, SideEffectLevel, ToolMeta
from prodagent.llm.fake import script
from prodagent.ports.llm import LLMClient
from prodagent.runtime.parent_runtime import ParentRuntime
from prodagent.tooling.base import FunctionTool

if TYPE_CHECKING:
    from prodagent.llm import LLMConfig


def _meta(name: str) -> ToolMeta:
    return ToolMeta(
        name=name,
        is_readonly=True,
        side_effect_level=SideEffectLevel.LOW,
        domain="test",
    )


class _CapturingLLM(LLMClient):
    def __init__(self, inner: LLMClient) -> None:
        self._inner = inner
        self.captured_messages: list[dict[str, Any]] = []

    async def complete(
        self,
        messages: list[dict[str, Any]],
        *,
        system: str = "",
        tools: list[dict[str, Any]] | None = None,
        config: LLMConfig | None = None,
        on_chunk: Any,
    ) -> LLMResponse:
        for msg in messages:
            if msg.get("role") == "user":
                self.captured_messages.append(dict(msg))
        return await self._inner.complete(
            messages,
            system=system,
            tools=tools,
            config=config,
            on_chunk=on_chunk,
        )

    def find_task_message(self) -> str:
        if not self.captured_messages:
            return ""
        return max(
            (str(m.get("content", "")) for m in self.captured_messages),
            key=len,
        )


def _child_with_tools(tool_names: list[str]) -> Agent:
    tools = [
        FunctionTool(name=n, fn=lambda: {"ok": 1}, meta=_meta(n), schema={}) for n in tool_names
    ]
    return Agent(
        "worker",
        system_prompt="do the work",
        tools=tools,
        mode=ExecutionMode.REACTIVE,
        config=AgentConfig(name="worker", description="worker"),
    )


@pytest.mark.asyncio
async def test_child_receives_packet_constraints_not_raw_task():
    fake = script({"content": "done"})
    capturing = _CapturingLLM(fake)
    child = _child_with_tools(["search_logs", "query_db"])

    spawn = build_spawn_tools_for_agent([child], llm=capturing, context=ParentRuntime())
    result = await spawn.tool._fn(name="worker", task="find the failing order")

    assert result["state"] != "duplicate"
    assert result["state"] != "contract_violation"

    assert capturing.captured_messages, (
        "child LLM was never called — spawn did not reach the child run"
    )

    task_content = capturing.find_task_message()

    assert "find the failing order" in task_content, (
        "child task must carry the parent's task description"
    )

    assert "search_logs" in task_content, (
        "child task must list the packet's available_tools — "
        "HandoffPacket was not delivered (finding #4 regression). "
        f"Got: {task_content!r}"
    )
    assert "query_db" in task_content, (
        "child task must list the packet's available_tools — "
        "HandoffPacket was not delivered (finding #4 regression). "
        f"Got: {task_content!r}"
    )

    assert "Available tools" in task_content, (
        "child task must be the packet's to_task_prompt() output, "
        "which includes an 'Available tools:' section. "
        f"Got: {task_content!r}"
    )


@pytest.mark.asyncio
async def test_child_receives_parent_constraints_not_phase_goal():
    fake = script({"content": "done"})
    capturing = _CapturingLLM(fake)
    child = _child_with_tools(["search"])

    spawn = build_spawn_tools_for_agent(
        [child],
        llm=capturing,
        context=ParentRuntime(constraints=["Never write to prod", "Budget capped at $1"]),
    )
    await spawn.tool._fn(name="worker", task="investigate")

    task_content = capturing.find_task_message()
    assert "Never write to prod" in task_content, (
        "child task must carry the parent's hard constraints — "
        "the packet's constraints field was not serialised. "
        f"Got: {task_content!r}"
    )
    assert "Budget capped at $1" in task_content, (
        f"child task must carry the parent's hard constraints. Got: {task_content!r}"
    )
    # The packet serialises constraints under a "Constraints:" header. When the
    # default memory bundle is also wired, it re-formats into "[CONSTRAINTS]" —
    # either form proves the constraints made it onto the child's task.
    assert "Constraint" in task_content or "CONSTRAINT" in task_content, (
        f"child task must include a constraints section from the packet. Got: {task_content!r}"
    )


@pytest.mark.asyncio
async def test_raw_task_not_passed_directly():
    fake = script({"content": "done"})
    capturing = _CapturingLLM(fake)
    child = _child_with_tools(["tool_a", "tool_b"])

    spawn = build_spawn_tools_for_agent([child], llm=capturing, context=ParentRuntime())
    raw_task = "just a bare task"
    await spawn.tool._fn(name="worker", task=raw_task)

    task_content = capturing.find_task_message()
    assert len(task_content) > len(raw_task), (
        "child task length equals the raw parent task — the packet was "
        "not serialised onto the child's task prompt (finding #4). "
        f"Got: {task_content!r}"
    )
