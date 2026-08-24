"""Sub-agent HITL suspension propagation.

When a spawned child agent hits a HIGH side-effect tool and suspends pending
approval, the parent's ``spawn_agent`` tool call must propagate the suspension
to the parent's run — not silently swallow it. The parent run parks with the
child's ``approval_request_id``; after approval, the parent retries the
``spawn_agent`` call, the child's deterministic ``run_id`` makes its
``PlanExecutor`` resume the DAG from the checkpoint.
"""

from __future__ import annotations

import pytest

from prodagent import Agent, AgentConfig, ExecutionMode, RunState, SideEffectLevel, ToolMeta
from prodagent.backends.file.checkpoint import FileCheckpointStore
from prodagent.core.config import FrameworkConfig
from prodagent.hooks.approval import ApprovalDecision, ApprovalGate
from prodagent.hooks.bundles.security import ApprovalHooks
from prodagent.kernel.bus import HookRegistry
from prodagent.llm.fake import script
from prodagent.plan.workflow import Workflow
from prodagent.tooling import tool


@tool(
    name="delete_record",
    meta=ToolMeta(
        name="delete_record",
        side_effect_level=SideEffectLevel.HIGH,
    ),
)
async def delete_record(record_id: str) -> dict:
    return {"deleted": record_id}


def _workflow_child(llm, gate: ApprovalGate, store, fw) -> Agent:
    """A workflow child whose DAG calls a HIGH side-effect tool → suspends."""
    wf = Workflow()
    wf.tool_step("s1", "delete_record", params={"record_id": "rec-42"})

    hooks = HookRegistry()
    return Agent(
        "wf_child",
        system_prompt="delete the record via DAG",
        tools=[delete_record],
        workflow=wf,
        allow_replan=False,
        config=AgentConfig(
            name="wf_child",
            llm=llm,
            hooks=hooks,
            framework=fw,
            extensions=[ApprovalHooks(gate=gate)],
        ),
    )


def _reactive_parent(child: Agent, llm, gate: ApprovalGate, store, fw) -> Agent:
    """REACTIVE parent that spawns the workflow child to do the deletion."""
    hooks = HookRegistry()
    return Agent(
        "parent",
        system_prompt="Spawn wf_child to delete the record.",
        mode=ExecutionMode.REACTIVE,
        config=AgentConfig(
            name="parent",
            llm=llm,
            hooks=hooks,
            framework=fw,
            agents=[child],
            extensions=[ApprovalHooks(gate=gate)],
        ),
    )


def _fw(tmp_path) -> FrameworkConfig:
    fw = FrameworkConfig.default()
    fw.orchestration.runs_dir = str(tmp_path / "runs")
    fw.orchestration.events_dir = str(tmp_path / "events")
    fw.orchestration.sessions_dir = str(tmp_path / "sessions")
    return fw


@pytest.mark.asyncio
async def test_child_suspension_propagates_to_parent_run(tmp_path):
    """Child hits HIGH tool → child suspends → parent run suspends with child's
    approval_request_id."""
    store = FileCheckpointStore(tmp_path / "ckpt")
    fw = _fw(tmp_path)
    gate = ApprovalGate()

    # Parent LLM: call spawn_agent once.
    parent_llm = script(
        {"tool": "spawn_agent", "params": {"name": "wf_child", "task": "delete rec-42"}},
    )
    # Child LLM: not used (workflow has no llm_step), but must be non-None.
    child_llm = script({"content": "noop"})

    child = _workflow_child(child_llm, gate, store, fw)
    parent = _reactive_parent(child, parent_llm, gate, store, fw)

    run = await parent.chat("delete rec-42", session_id="parent-run-1")

    assert run.state is RunState.SUSPENDED, (
        f"parent should suspend when child suspends, got {run.state}"
    )
    assert run.pending_approval_id, "parent run must carry the child's approval_request_id"
    assert run.pending_tool_call is not None
    assert run.pending_tool_call.name == "spawn_agent"


@pytest.mark.asyncio
async def test_after_approval_parent_resumes_and_child_completes(tmp_path):
    """Full loop: parent runs → child suspends → approve → parent resumes →
    child DAG completes → parent completes."""
    store = FileCheckpointStore(tmp_path / "ckpt")
    fw = _fw(tmp_path)
    gate = ApprovalGate()

    # Parent LLM: spawn_agent → then summarize.
    parent_llm = script(
        {"tool": "spawn_agent", "params": {"name": "wf_child", "task": "delete rec-42"}},
        {"content": "Record deleted via child workflow."},
    )
    child_llm = script({"content": "noop"})

    child = _workflow_child(child_llm, gate, store, fw)
    parent = _reactive_parent(child, parent_llm, gate, store, fw)

    # Turn 1: parent spawns child, child hits HIGH tool, both suspend.
    run1 = await parent.chat("delete rec-42", session_id="parent-run-2")
    assert run1.state is RunState.SUSPENDED
    request_id = run1.pending_approval_id

    # Approve the child's HIGH tool call.
    await gate.submit_decision(request_id, ApprovalDecision.APPROVE)

    # Turn 2: resume — parent retries spawn_agent, child resumes DAG, completes.
    run2 = await parent.chat(resume=True, session_id="parent-run-2")

    assert run2.state is RunState.COMPLETED, (
        f"parent should complete after approval, got {run2.state} (last_error={run2.last_error})"
    )


@pytest.mark.asyncio
async def test_child_approval_request_id_is_real_not_empty(tmp_path):
    """The approval_request_id propagated to the parent must be the child's
    actual request_id (a UUID), not an empty string."""
    store = FileCheckpointStore(tmp_path / "ckpt")
    fw = _fw(tmp_path)
    gate = ApprovalGate()

    parent_llm = script(
        {"tool": "spawn_agent", "params": {"name": "wf_child", "task": "delete"}},
    )
    child_llm = script({"content": "noop"})

    child = _workflow_child(child_llm, gate, store, fw)
    parent = _reactive_parent(child, parent_llm, gate, store, fw)

    run = await parent.chat("delete", session_id="parent-run-3")

    assert run.state is RunState.SUSPENDED
    assert run.pending_approval_id, "request_id must not be empty"
    assert len(run.pending_approval_id) >= 8, (
        f"request_id looks like a real UUID, got {run.pending_approval_id!r}"
    )
