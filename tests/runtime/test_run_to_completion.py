from __future__ import annotations

import asyncio

from prodagent import Agent, AgentConfig, ExecutionMode, RunState, SideEffectLevel, ToolMeta
from prodagent.backends.file.checkpoint import FileCheckpointStore
from prodagent.hooks.approval import ApprovalGate
from prodagent.hooks.bundles.security import ApprovalHooks
from prodagent.kernel.bus import HookRegistry
from prodagent.llm.fake import script
from prodagent.tooling import tool


@tool(
    name="restart_pod",
    meta=ToolMeta(name="restart_pod", side_effect_level=SideEffectLevel.HIGH),
)
async def restart_pod(service: str) -> dict:
    return {"restarted": service}


def _high_tool_agent(llm, gate: ApprovalGate, *, store) -> Agent:
    return Agent(
        name="ops",
        system_prompt="Restart the pod.",
        tools=[restart_pod],
        mode=ExecutionMode.REACTIVE,
        config=AgentConfig(
            name="ops",
            llm=llm,
            hooks=HookRegistry(),
            checkpoint=store,
            extensions=[ApprovalHooks(gate=gate)],
        ),
    )


async def _drive_to_completion(
    agent: Agent, task: str, *, session_id: str, decision: str = "approve"
) -> RunState:
    run = await agent.chat(task, session_id=session_id)
    while run.state is RunState.SUSPENDED and run.pending_approval_id:
        await agent.submit_approval(run.pending_approval_id, decision)
        run = await agent.chat(resume=True, session_id=session_id)
    return run


def test_chat_auto_approve_loop_finishes(tmp_path):
    store = FileCheckpointStore(tmp_path)
    gate = ApprovalGate()

    llm = script(
        {"tool": "restart_pod", "params": {"service": "api"}},
        {"content": "Pod restarted."},
    )
    agent = _high_tool_agent(llm, gate, store=store)

    run = asyncio.run(_drive_to_completion(agent, "restart api", session_id="run-auto"))

    assert run.state is RunState.COMPLETED
    assert any(c.name == "restart_pod" for c in run.tool_history)


def test_chat_auto_approve_loop_with_reject_soft_veto_completes(tmp_path):
    store = FileCheckpointStore(tmp_path)
    gate = ApprovalGate()

    llm = script(
        {"tool": "restart_pod", "params": {"service": "api"}},
        {"content": "Could not restart; approval denied."},
    )
    agent = _high_tool_agent(llm, gate, store=store)

    run = asyncio.run(
        _drive_to_completion(agent, "restart api", session_id="run-reject", decision="reject")
    )

    assert run.state is RunState.COMPLETED
