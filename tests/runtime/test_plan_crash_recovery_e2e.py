from __future__ import annotations

import asyncio
import json
from typing import TYPE_CHECKING

import pytest

from prodagent import ExecutionMode
from prodagent.backends.file.checkpoint import FileCheckpointStore
from prodagent.backends.file.event_log import FileEventLog
from prodagent.core.event_log import PlanEventType
from prodagent.core.types import LLMResponse, RunState
from prodagent.hooks.registry import HookRegistry
from prodagent.llm.fake import FakeLLMAdapter
from prodagent.runtime.agent import Agent
from prodagent.runtime.config import AgentConfig
from prodagent.tooling import tool

if TYPE_CHECKING:
    from pathlib import Path

RUN_ID = "INC-123"
RUN_ID_V1 = f"{RUN_ID}:1"

PLAN = {
    "steps": [
        {"id": "diagnose", "action": "collect_logs", "params": {"pod": "api-7"}, "depends_on": []},
        {
            "id": "remediate",
            "action": "restart_pod",
            "params": {"pod": "{{diagnose.output.pod}}"},
            "depends_on": ["diagnose"],
        },
        {
            "id": "verify",
            "action": "check_health",
            "params": {"pod": "api-7"},
            "depends_on": ["remediate"],
        },
        {
            "id": "notify",
            "action": "post_report",
            "params": {"incident": "INC-123"},
            "depends_on": ["verify"],
            "terminal": True,
        },
    ]
}


def _plan_llm() -> FakeLLMAdapter:
    return FakeLLMAdapter(responses=[LLMResponse(content=json.dumps(PLAN), stop_reason="end_turn")])


def _stores(tmp_path: Path) -> tuple[FileEventLog, FileCheckpointStore]:
    return (
        FileEventLog(tmp_path / "events"),
        FileCheckpointStore(directory=tmp_path / "checkpoints"),
    )


async def _drain(agent: Agent, task: str, session_id: str, *, resume: bool = False) -> None:
    async for _ in agent.chat_stream(task, session_id=session_id, resume=resume):
        pass


@pytest.mark.asyncio
async def test_plan_crash_recovery_resumes_from_last_checkpoint(tmp_path):

    events, checkpoints = _stores(tmp_path)
    calls: list[str] = []

    @tool(name="collect_logs", readonly=True)
    async def collect_logs(pod: str) -> dict:
        calls.append("collect_logs")
        return {"pod": pod, "root_cause": "oom-killed"}

    @tool(name="restart_pod")
    async def restart_pod(pod: str) -> dict:
        calls.append("restart_pod")
        if calls.count("restart_pod") == 1:
            raise asyncio.CancelledError("process killed mid-step")
        return {"restarted": True, "pod": pod}

    @tool(name="check_health", readonly=True)
    async def check_health(pod: str) -> dict:
        calls.append("check_health")
        return {"healthy": True, "pod": pod}

    @tool(name="post_report", readonly=True)
    async def post_report(incident: str) -> dict:
        calls.append("post_report")
        return {"report": f"{incident} resolved: pod restarted"}

    def _production_fw():
        from prodagent.core.config import FrameworkConfig, production

        return production(FrameworkConfig.default())

    def _make_agent() -> Agent:
        return Agent(
            name="aiops",
            system_prompt="Remediate incidents.",
            tools=[collect_logs, restart_pod, check_health, post_report],
            mode=ExecutionMode.PLAN_FIRST,
            config=AgentConfig(
                name="aiops",
                llm=_plan_llm(),
                hooks=HookRegistry(),
                checkpoint=checkpoints,
                event_log=events,
                framework=_production_fw(),
            ),
        )

    agent1 = _make_agent()
    with pytest.raises(asyncio.CancelledError):
        await _drain(agent1, "remediate INC-123", RUN_ID)

    assert calls == ["collect_logs", "restart_pod"], (
        f"Run 1 should have called collect_logs + restart_pod(crash), got {calls}"
    )

    types_run1 = [e.event_type for e in await events.get_events(RUN_ID_V1)]
    assert types_run1 == [
        PlanEventType.PLAN_CREATED,
        PlanEventType.STEP_STARTED,
        PlanEventType.STEP_COMPLETED,
        PlanEventType.STEP_STARTED,
    ], f"unexpected event sequence after crash: {types_run1}"

    agent2 = _make_agent()
    await _drain(agent2, "remediate INC-123", RUN_ID, resume=True)

    assert calls == ["collect_logs", "restart_pod", "restart_pod", "check_health", "post_report"], (
        f"Run 2 should resume from restart_pod, got {calls}"
    )

    run2 = await checkpoints.load(RUN_ID_V1)
    assert run2 is not None, "checkpoint should exist for the resumed run"
    assert run2.state is RunState.COMPLETED
    assert run2.final_output is not None
    assert "INC-123 resolved" in run2.final_output
    assert "pod restarted" in run2.final_output

    types_full = [e.event_type for e in await events.get_events(RUN_ID_V1)]
    assert PlanEventType.STEP_FAILED not in types_full
    assert PlanEventType.PLAN_REPLANNED not in types_full
    assert types_full.count(PlanEventType.PLAN_CREATED) == 1
    assert types_full.count(PlanEventType.STEP_COMPLETED) == 4
    assert types_full.count(PlanEventType.STEP_STARTED) == 5
