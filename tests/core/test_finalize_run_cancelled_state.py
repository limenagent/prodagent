from __future__ import annotations

import asyncio

import pytest

from prodagent.core.types import RunState
from prodagent.hooks.events import HookEvent
from prodagent.hooks.registry import HookRegistry
from prodagent.llm.fake import FakeLLMAdapter
from prodagent.runtime.agent import Agent


async def test_cancelled_run_reports_failed_state() -> None:
    from prodagent.core.types import LLMResponse

    captured: dict = {}

    def _on_session_end(*, state: str = "", turns: int = 0, **_):
        captured["state"] = state
        captured["turns"] = turns

    hooks = HookRegistry()
    hooks.register_event(HookEvent.SESSION_END, _on_session_end)

    async def _block_forever(**_):
        await asyncio.sleep(300)
        return {"status": "ok"}

    from prodagent.core.types import SideEffectLevel, ToolMeta
    from prodagent.tooling.base import FunctionTool

    tool = FunctionTool(
        name="block",
        fn=_block_forever,
        meta=ToolMeta(
            name="block",
            is_readonly=True,
            side_effect_level=SideEffectLevel.LOW,
            estimated_latency_ms=600_000,
        ),
        schema={
            "name": "block",
            "description": "blocks",
            "input_schema": {"type": "object", "properties": {}},
        },
    )

    plan_json = '{"steps": [{"id": "s1", "action": "block", "params": {}, "depends_on": []}]}'
    agent = Agent(
        "cancel_me",
        context="plan something",
        tools=[tool],
        llm=FakeLLMAdapter(responses=[LLMResponse(content=plan_json, stop_reason="end_turn")]),
        hooks=hooks,
    )

    run_task = asyncio.create_task(agent.chat("do something", session_id="cancel-test"))
    await asyncio.sleep(1.0)
    run_task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await run_task

    assert captured.get("state") == RunState.FAILED.value, (
        f"cancelled run must report FAILED, got {captured.get('state')!r}"
    )
