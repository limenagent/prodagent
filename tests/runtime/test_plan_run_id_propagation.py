"""PLAN_FIRST must propagate the real run_id into tool dispatch.

Regression: ``StepRunner`` used to call ``tool_executor(call)`` without
``run_id`` — ``dispatcher.dispatch`` then defaulted to ``run_id=""``, so every
tool-layer hook payload and every ``inject_run_id`` tool saw an empty run_id
in PLAN_FIRST mode while REACTIVE worked. The two execution modes must not
drift on observable semantics.
"""

from __future__ import annotations

import json
from typing import Any

from prodagent.backends.file.checkpoint import FileCheckpointStore
from prodagent.backends.file.event_log import FileEventLog
from prodagent.hooks import HookRegistry
from prodagent.kernel.bus import HookEvent
from prodagent.kernel.events import RunCompletedEvent
from prodagent.kernel.types import LLMResponse, ToolMeta
from prodagent.llm.fake import FakeLLMAdapter
from prodagent.plan.executor import PlanExecutor
from prodagent.tooling.base import FunctionTool
from prodagent.tooling.dispatcher import ToolDispatcher


def _plan_llm() -> FakeLLMAdapter:
    plan = {"steps": [{"id": "s1", "action": "echo_run_id", "params": {}, "depends_on": []}]}
    return FakeLLMAdapter(responses=[LLMResponse(content=json.dumps(plan), stop_reason="end_turn")])


def _echo_run_id_tool() -> FunctionTool:
    async def fn(run_id: str = "") -> str:
        return f"run_id={run_id}"

    return FunctionTool(
        name="echo_run_id",
        fn=fn,
        meta=ToolMeta(name="echo_run_id", is_readonly=True),
        schema={
            "name": "echo_run_id",
            "description": "echo the injected run_id",
            "parameters": {"type": "object", "properties": {}},
        },
        inject_run_id=True,
    )


async def test_plan_first_propagates_run_id_to_hooks_and_tools(tmp_path):
    hook_run_ids: list[str] = []

    hooks = HookRegistry()

    async def record_tool_call(**payload: Any) -> None:
        hook_run_ids.append(payload["run_id"])

    hooks.register_event(HookEvent.TOOL_CALL, record_tool_call)

    dispatcher = ToolDispatcher({"echo_run_id": _echo_run_id_tool()}, hooks=hooks)

    executor = PlanExecutor(
        _plan_llm(),
        dispatcher.dispatch,
        system="sys",
        messages=[{"role": "user", "content": "do"}],
        hooks=hooks,
        agent_name="plan-agent",
        event_log=FileEventLog(tmp_path / "events"),
        checkpoint_store=FileCheckpointStore(directory=tmp_path / "checkpoints"),
    )

    streamed: list[Any] = []
    async for event in executor.stream("do", run_id="run-prop-1"):
        streamed.append(event)

    assert any(isinstance(e, RunCompletedEvent) for e in streamed)
    # Hook payloads saw the real run_id, never the "" default.
    assert hook_run_ids, "TOOL_CALL hook never fired"
    assert all(rid == "run-prop-1" for rid in hook_run_ids), hook_run_ids
