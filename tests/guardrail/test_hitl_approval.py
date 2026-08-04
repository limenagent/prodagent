from __future__ import annotations

import asyncio
import contextlib

from prodagent import RunState, SideEffectLevel, ToolMeta
from prodagent.backends.file.checkpoint import FileCheckpointStore
from prodagent.guardrail.approval import ApprovalDecision, ApprovalGate
from prodagent.hooks.bundles.security import ApprovalHooks
from prodagent.hooks.registry import HookRegistry
from prodagent.llm.fake import script
from prodagent.runtime.agent import Agent
from prodagent.tooling import tool


@tool(
    name="restart_pod",
    meta=ToolMeta(name="restart_pod", side_effect_level=SideEffectLevel.HIGH, reversibility=0.1),
)
async def restart_pod(service: str) -> dict:
    return {"restarted": service}


def _high_tool_agent(llm, hitl: ApprovalHooks, *, store=None) -> Agent:
    return (
        Agent(
            name="ops",
            context="Restart the pod.",
            tools=[restart_pod],
            llm=llm,
            hooks=HookRegistry(),
            checkpoint=store,
        )
        .reactive()
        .extend(hitl)
    )


def test_reject_blocks_high_tool(tmp_path):
    """Pre-submit REJECT, then resume: tool history records the blocked call."""
    store = FileCheckpointStore(tmp_path)
    gate = ApprovalGate()
    hitl = ApprovalHooks(gate=gate)

    llm1 = script({"tool": "restart_pod", "params": {"service": "api"}})
    agent1 = _high_tool_agent(llm1, hitl, store=store)
    run1 = asyncio.run(agent1.chat("restart api", session_id="run-reject"))
    assert run1.state == RunState.SUSPENDED
    assert run1.pending_approval_id

    asyncio.run(
        gate.submit_decision(
            run1.pending_approval_id, ApprovalDecision.REJECT, approver_id="tester"
        )
    )

    llm2 = script({"content": "Could not restart; approval denied."})
    agent2 = _high_tool_agent(llm2, hitl, store=store)
    run2 = asyncio.run(agent2.chat(resume=True, session_id="run-reject"))

    assert run2.state == RunState.COMPLETED
    blocked = [c for c in run2.tool_history if c.name == "restart_pod"]
    assert blocked, "restart_pod should appear in tool history even when rejected"


def test_resume_after_approval_reexecutes_pending_call(tmp_path):
    store = FileCheckpointStore(tmp_path)

    gate = ApprovalGate()
    hitl = ApprovalHooks(gate=gate)
    llm1 = script({"tool": "restart_pod", "params": {"service": "api"}})
    agent1 = _high_tool_agent(llm1, hitl, store=store)
    run1 = asyncio.run(agent1.chat("restart api", session_id="run-hitl-resume"))

    assert run1.state == RunState.SUSPENDED
    assert run1.pending_tool_call is not None
    assert run1.pending_tool_call.name == "restart_pod"
    assert not any(c.name == "restart_pod" for c in run1.tool_history)

    saved = asyncio.run(store.load("run-hitl-resume:1"))
    assert saved is not None
    assert saved.pending_tool_call is not None
    assert saved.pending_tool_call.name == "restart_pod"
    assert any(m.get("role") == "assistant" and m.get("tool_calls") for m in saved.messages), (
        "the assistant's tool_call request must survive the checkpoint"
    )

    asyncio.run(gate.submit_decision(run1.pending_approval_id, ApprovalDecision.FULL_APPROVAL))

    llm2 = script({"content": "Pod restarted."})
    agent2 = _high_tool_agent(llm2, hitl, store=store)
    run2 = asyncio.run(agent2.chat(resume=True, session_id="run-hitl-resume"))

    assert run2.state == RunState.COMPLETED
    assert run2.pending_tool_call is None
    assert any(c.name == "restart_pod" for c in run2.tool_history)
    assert llm2.call_count == 1


def test_no_bundle_suspends_high_tool():
    llm = script({"tool": "restart_pod", "params": {"service": "api"}})
    agent = Agent(
        name="ops",
        context="Restart.",
        tools=[restart_pod],
        llm=llm,
        hooks=HookRegistry(),
    ).reactive()
    run = asyncio.run(agent.chat("restart api"))
    assert run.state == RunState.SUSPENDED
    assert not any(c.name == "restart_pod" for c in run.tool_history)


def test_gate_request_forces_full_approval_when_confidence_is_none():
    import pytest

    from prodagent.core.exceptions import SuspendPendingApproval

    gate = ApprovalGate()
    hitl = ApprovalHooks(gate=gate)

    async def _call_without_confidence() -> None:
        await hitl.gate_request(
            name="restart_pod",
            params={"service": "api"},
            confidence=None,
            meta=ToolMeta(
                name="restart_pod", side_effect_level=SideEffectLevel.HIGH, reversibility=0.1
            ),
            run_id="test-run",
        )

    with pytest.raises(SuspendPendingApproval):
        asyncio.run(_call_without_confidence())


def test_gate_request_accepts_explicit_confidence():
    gate = ApprovalGate()
    hitl = ApprovalHooks(gate=gate)

    async def _call_with_confidence() -> None:
        with contextlib.suppress(Exception):
            await hitl.gate_request(
                name="restart_pod",
                params={"service": "api"},
                confidence=0.3,
                meta=ToolMeta(
                    name="restart_pod", side_effect_level=SideEffectLevel.HIGH, reversibility=0.1
                ),
                run_id="test-run",
            )

    asyncio.run(_call_with_confidence())


def test_missing_confidence_forces_full_approval_under_fail_open_policy():
    import pytest

    from prodagent.core.exceptions import SuspendPendingApproval
    from prodagent.hooks.checkpoint import CheckPoint
    from prodagent.hooks.registry import FailurePolicy, HookRegistry

    gate = ApprovalGate()
    hitl = ApprovalHooks(gate=gate)

    hooks = HookRegistry(failure_policy=FailurePolicy.FAIL_OPEN)
    hitl.attach(hooks)

    async def _dispatch_without_confidence() -> None:
        await hooks.check_blocking(
            CheckPoint.APPROVAL_REQUEST,
            name="restart_pod",
            params={"service": "api"},
            confidence=None,
            meta=ToolMeta(
                name="restart_pod", side_effect_level=SideEffectLevel.HIGH, reversibility=0.1
            ),
            run_id="test-run",
        )

    with pytest.raises(SuspendPendingApproval):
        asyncio.run(_dispatch_without_confidence())


def test_resume_then_new_high_call_does_not_reuse_stale_approval_id(tmp_path):
    """After a deferred decision is consumed on resume, the dispatcher must
    clear its ``_pending_approval_id`` — otherwise the next HIGH tool call
    (in the same run) leaks the stale id into ``gate.evaluate``, which logs
    "no deferred decision found" and re-suspends with a fresh id (forced
    double-approval in the UI).

    Regression for the trader playground scenario: server restart between
    suspend and approve wipes in-memory tool state, so the resumed HIGH call
    fails; LLM re-proposes and calls the HIGH tool again within the same run,
    which hit the bug.

    Tested at the dispatcher level so we don't need to drive a full agent
    loop through suspend→resume→re-call.
    """
    import logging

    from prodagent.core.types import ToolCall
    from prodagent.hooks.registry import HookRegistry
    from prodagent.tooling.dispatcher import ToolDispatcher

    gate = ApprovalGate()
    hitl = ApprovalHooks(gate=gate)
    hooks = HookRegistry()
    hitl.attach(hooks)

    call = ToolCall(name="restart_pod", params={"service": "api"})
    dispatcher = ToolDispatcher(
        {"restart_pod": restart_pod},
        hooks=hooks,
        agent_id="ops",
    )

    r1 = asyncio.run(dispatcher.dispatch(call))
    assert r1.outcome.value == "suspended"
    first_request_id = r1.approval_request_id
    assert first_request_id

    asyncio.run(gate.submit_decision(first_request_id, ApprovalDecision.FULL_APPROVAL))
    dispatcher.set_pending_approval_id(first_request_id)

    r2 = asyncio.run(dispatcher.dispatch(call))
    assert r2.outcome.value == "ok", f"resume should have run the tool, got {r2.outcome}"

    gate_logs: list[str] = []

    class _Capture(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            gate_logs.append(record.getMessage())

    capture = _Capture(level=logging.WARNING)
    gate_logger = logging.getLogger("prodagent.guardrail.approval.gate")
    gate_logger.addHandler(capture)
    try:
        r3 = asyncio.run(dispatcher.dispatch(call))
    finally:
        gate_logger.removeHandler(capture)

    assert r3.outcome.value == "suspended", "third call should suspend for a fresh approval"
    assert r3.approval_request_id != first_request_id
    assert not any("no deferred decision found" in m for m in gate_logs), (
        f"dispatcher leaked the stale pending_approval_id into the next HIGH call; "
        f"gate logs: {gate_logs}"
    )
