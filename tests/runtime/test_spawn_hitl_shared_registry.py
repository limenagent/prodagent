"""Sub-agent HITL with a *shared* hook registry (default wiring).

Sibling to ``test_spawn_hitl_propagation.py``. That module gives the parent
and child **separate** ``HookRegistry()`` instances, which sidesteps the
duplicate-checker bug. This module exercises the other path: parent and
child share one ``ApprovalGate`` AND rely on **default** hook wiring (neither
passes ``hooks=``), so the spawned child inherits the parent's registry.

Without idempotent registration this double-registers ``ApprovalHooks`` on
``APPROVAL_REQUEST``: one ``check_blocking`` call evaluates the gate twice,
the second pass finds the deferred decision already consumed and re-requests
→ the run never advances past approval. These tests pin the fix.
"""

from __future__ import annotations

import pytest

from prodagent import Agent, ExecutionMode, RunState, SideEffectLevel, ToolMeta
from prodagent.core.config import FrameworkConfig
from prodagent.guardrail.approval import ApprovalDecision, ApprovalGate
from prodagent.hooks.bundles.security import ApprovalHooks
from prodagent.llm.fake import script
from prodagent.runtime.workflow import Workflow
from prodagent.tooling import tool


@tool(
    name="delete_record",
    meta=ToolMeta(
        name="delete_record",
        side_effect_level=SideEffectLevel.HIGH,
        reversibility=0.0,
    ),
)
async def delete_record(record_id: str) -> dict:
    return {"deleted": record_id}


def _workflow_child(llm, gate: ApprovalGate, fw) -> Agent:
    """Workflow child whose DAG calls a HIGH tool → suspends.

    Deliberately has NO explicit ``hooks=`` — it relies on default wiring, so
    when spawned it inherits the parent's HookRegistry (the shared-gate,
    shared-registry case).
    """
    wf = Workflow()
    wf.tool_step("s1", "delete_record", params={"record_id": "rec-42"})
    return Agent(
        "wf_child",
        system_prompt="delete the record via DAG",
        tools=[delete_record],
        llm=llm,
        framework=fw,
        workflow=wf,
        allow_replan=False,
        extensions=[ApprovalHooks(gate=gate)],
    )


def _reactive_parent(child: Agent, llm, gate: ApprovalGate, fw) -> Agent:
    """REACTIVE parent that spawns the workflow child. Also default-wired."""
    return Agent(
        "parent",
        system_prompt="Spawn wf_child to delete the record.",
        llm=llm,
        framework=fw,
        mode=ExecutionMode.REACTIVE,
        agents=[child],
        extensions=[ApprovalHooks(gate=gate)],
    )


def _fw(tmp_path) -> FrameworkConfig:
    fw = FrameworkConfig.default()
    fw.orchestration.runs_dir = str(tmp_path / "runs")
    fw.orchestration.events_dir = str(tmp_path / "events")
    fw.orchestration.sessions_dir = str(tmp_path / "sessions")
    return fw


@pytest.mark.asyncio
async def test_shared_registry_child_suspends_then_completes_after_one_approval(tmp_path):
    """Full loop on the shared-registry path: parent runs → child suspends →
    approve → parent resumes → child DAG completes → parent completes.

    Regression: previously this re-suspended forever because the shared gate
    was registered twice on the shared registry.
    """
    fw = _fw(tmp_path)
    gate = ApprovalGate()

    parent_llm = script(
        {"tool": "spawn_agent", "params": {"name": "wf_child", "task": "delete rec-42"}},
        {"content": "Record deleted via child workflow."},
    )
    child_llm = script({"content": "noop"})

    child = _workflow_child(child_llm, gate, fw)
    parent = _reactive_parent(child, parent_llm, gate, fw)

    # Turn 1: parent spawns child, child hits HIGH tool, both suspend.
    run1 = await parent.chat("delete rec-42", session_id="shared-registry-1")
    assert run1.state is RunState.SUSPENDED
    request_id = run1.pending_approval_id
    assert request_id

    # Approve the child's HIGH tool call.
    await gate.submit_decision(request_id, ApprovalDecision.FULL_APPROVAL)

    # Turn 2: resume — must complete, not re-suspend.
    run2 = await parent.chat(resume=True, session_id="shared-registry-1")

    assert run2.state is RunState.COMPLETED, (
        f"parent should complete after one approval, got {run2.state} "
        f"(last_error={run2.last_error})"
    )


@pytest.mark.asyncio
async def test_gate_registers_one_checker_per_registry(tmp_path):
    """Attaching two ApprovalHooks that share a gate to the same registry
    yields exactly one APPROVAL_REQUEST checker (the dedup invariant)."""
    from prodagent.hooks.checkpoint import CheckPoint
    from prodagent.hooks.registry import HookRegistry

    gate = ApprovalGate()
    registry = HookRegistry()

    ApprovalHooks(gate=gate).attach(registry)
    ApprovalHooks(gate=gate).attach(registry)  # parent's + child's, same gate

    handlers = registry._check_handlers[CheckPoint.APPROVAL_REQUEST]
    assert len(handlers) == 1, f"expected one checker, got {len(handlers)}"
