from __future__ import annotations

import asyncio
import json

import pytest

from prodagent.backends.file.checkpoint import FileCheckpointStore
from prodagent.backends.file.event_log import FileEventLog
from prodagent.kernel.events import RunCompletedEvent, RunFailedEvent, RunSuspendedEvent
from prodagent.kernel.types import LLMResponse, ToolOutcome, ToolResult
from prodagent.llm.fake import FakeLLMAdapter
from prodagent.plan.executor import PlanExecutor

_TERMINAL = (RunCompletedEvent, RunFailedEvent, RunSuspendedEvent)


def _final_run(events: list) -> object:
    for event in reversed(events):
        if isinstance(event, _TERMINAL):
            return event.run
    raise AssertionError("stream produced no terminal event")


def _plan_llm(*plans: dict) -> FakeLLMAdapter:
    return FakeLLMAdapter(
        responses=[LLMResponse(content=json.dumps(p), stop_reason="end_turn") for p in plans]
    )


def _stores(tmp_path):
    return (
        FileEventLog(tmp_path / "events"),
        FileCheckpointStore(directory=tmp_path / "checkpoints"),
    )


class _SuspendOnCall:
    def __init__(self, trigger: str) -> None:
        self._trigger = trigger
        self.calls: list[str] = []

    async def __call__(self, call, *, run_id: str = "") -> dict:
        self.calls.append(call.name)
        if call.name == self._trigger:
            from prodagent.core.exceptions import SuspendPendingApproval

            raise SuspendPendingApproval(f"tool '{call.name}' suspended", tool=call.name)
        return {"status": "ok", "action": call.name}


@pytest.mark.asyncio
async def test_suspend_completes_sibling_steps(tmp_path):
    events, checkpoints = _stores(tmp_path)
    executor = _SuspendOnCall(trigger="b")
    planner = PlanExecutor(
        _plan_llm(
            {
                "steps": [
                    {"id": "a", "action": "a", "params": {}, "depends_on": []},
                    {"id": "b", "action": "b", "params": {}, "depends_on": []},
                ]
            }
        ),
        executor,
        system="sys",
        messages=[{"role": "user", "content": "do"}],
        event_log=events,
        checkpoint_store=checkpoints,
    )

    streamed: list = []
    async for event in planner.stream("do", run_id="R1"):
        streamed.append(event)

    run = _final_run(streamed)
    assert run.state.value == "suspended"
    assert set(executor.calls) == {"a", "b"}
    assert any(c.name == "a" for c in run.tool_history), "sibling 'a' missing from tool_history"


@pytest.mark.asyncio
async def test_suspend_step_is_suspended_not_running(tmp_path):
    events, checkpoints = _stores(tmp_path)
    planner = PlanExecutor(
        _plan_llm(
            {
                "steps": [
                    {"id": "a", "action": "a", "params": {}, "depends_on": []},
                    {"id": "b", "action": "b", "params": {}, "depends_on": []},
                ]
            }
        ),
        _SuspendOnCall(trigger="b"),
        system="sys",
        messages=[{"role": "user", "content": "do"}],
        event_log=events,
        checkpoint_store=checkpoints,
    )

    streamed: list = []
    async for event in planner.stream("do", run_id="R2"):
        streamed.append(event)

    run = _final_run(streamed)
    state = await planner._log.restore_plan(run)
    assert state["steps"]["b"]["status"] == "suspended"
    assert state["steps"]["a"]["status"] == "completed"


@pytest.mark.asyncio
async def test_resume_after_suspend_does_not_reexecute_suspended_step(tmp_path):
    events, checkpoints = _stores(tmp_path)
    executor = _SuspendOnCall(trigger="b")
    planner = PlanExecutor(
        _plan_llm(
            {
                "steps": [
                    {"id": "a", "action": "a", "params": {}, "depends_on": []},
                    {"id": "b", "action": "b", "params": {}, "depends_on": []},
                ]
            }
        ),
        executor,
        system="sys",
        messages=[{"role": "user", "content": "do"}],
        event_log=events,
        checkpoint_store=checkpoints,
    )

    async for _ in planner.stream("do", run_id="R3"):
        pass
    calls_before = len(executor.calls)

    async for _ in planner.stream("do", run_id="R3"):
        pass

    assert len(executor.calls) == calls_before, "suspended step was re-executed on resume"


@pytest.mark.asyncio
async def test_resume_after_approval_reexecutes_suspended_step(tmp_path):
    """After approve, the deferred decision sits in the gate; resume must re-queue
    the SUSPENDED step as PENDING so it re-executes and consumes that decision.
    Without requeue, the plan stalls at 0 turns — SUSPENDED is invisible to
    get_parallel_ready, so the executor returns immediately as COMPLETED.

    Uses Agent + RunOrchestrator (not bare PlanExecutor) so the ToolDispatcher
    is wired — step-level approval needs dispatcher._pending_approval_id synced
    from run.pending_approval_id on resume."""
    import json

    from prodagent import Agent, AgentConfig, ExecutionMode, SideEffectLevel, ToolMeta
    from prodagent.hooks.approval import ApprovalGate
    from prodagent.hooks.bundles.security import ApprovalHooks
    from prodagent.kernel.bus import HookRegistry
    from prodagent.kernel.types import LLMResponse
    from prodagent.llm.fake import FakeLLMAdapter
    from prodagent.tooling import tool

    call_log: list[str] = []

    @tool(
        name="rollback",
        meta=ToolMeta(name="rollback", side_effect_level=SideEffectLevel.HIGH),
    )
    async def rollback(service: str) -> dict:
        call_log.append("rollback")
        return {"rolled_back": service}

    @tool(
        name="verify",
        meta=ToolMeta(name="verify", side_effect_level=SideEffectLevel.LOW),
    )
    async def verify(service: str) -> dict:
        call_log.append("verify")
        return {"ok": True, "service": service}

    gate = ApprovalGate()
    plan = {
        "steps": [
            {"id": "s1", "action": "rollback", "params": {"service": "api"}, "depends_on": []},
            {
                "id": "s2",
                "action": "verify",
                "params": {"service": "api"},
                "depends_on": ["s1"],
                "terminal": True,
            },
        ]
    }
    llm = FakeLLMAdapter(responses=[LLMResponse(content=json.dumps(plan), stop_reason="end_turn")])
    store = FileCheckpointStore(tmp_path / "cp")
    from prodagent.backends.file.event_log import FileEventLog

    events = FileEventLog(tmp_path / "events")

    agent = Agent(
        name="remediator",
        system_prompt="Fix the incident.",
        tools=[rollback, verify],
        mode=ExecutionMode.PLAN_FIRST,
        config=AgentConfig(
            name="remediator",
            llm=llm,
            hooks=HookRegistry(),
            checkpoint=store,
            event_log=events,
            extensions=[ApprovalHooks(gate=gate)],
        ),
    )
    assert agent.mode is ExecutionMode.PLAN_FIRST

    session_id = "resume-approval-test"

    # First run: s1 (rollback, HIGH) suspends at the approval gate before executing;
    # s2 must not run.
    events1: list = []
    async for event in agent.chat_stream("fix api", session_id=session_id):
        events1.append(event)
    run1 = _final_run(events1)
    assert run1.state.value == "suspended", f"expected suspended, got {run1.state.value}"
    assert call_log.count("rollback") == 0, "HIGH tool must not execute before approval"
    assert "verify" not in call_log, "downstream of suspended step must not run"
    assert run1.pending_approval_id is not None

    # Approve: deferred decision recorded in gate.
    await agent.submit_approval(run1.pending_approval_id, "approve", approver_id="test")

    # Resume: rollback re-executes (consuming deferred decision), verify runs.
    events2: list = []
    async for event in agent.chat_stream(session_id=session_id, resume=True):
        events2.append(event)
    run2 = _final_run(events2)
    assert run2.state.value == "completed", f"expected completed, got {run2.state.value}"
    assert call_log.count("rollback") >= 1, "suspended step was not executed on resume"
    assert "verify" in call_log, "downstream step must run after suspended step completes"


class _HandoffOnA:
    """Parallel steps a+b: a hands off, b finishes afterwards (after the
    handoff is parked) — b must not commit its transcript into the run."""

    def __init__(self) -> None:
        self.calls: list[str] = []

    async def __call__(self, call, *, run_id: str = "") -> ToolResult:
        self.calls.append(call.name)
        if call.name == "a":
            return ToolResult.for_handoff(peer="peer_x", task="take over", tool="a")
        await asyncio.sleep(0.05)
        return ToolResult(ToolOutcome.OK, value={"action": "b"}, tool="b")


@pytest.mark.asyncio
async def test_handoff_wins_concurrent_sibling_does_not_commit(tmp_path):
    """A peer handoff parked by one parallel step must stop the other step
    from committing its tool message into the run transcript."""
    events, checkpoints = _stores(tmp_path)
    executor = _HandoffOnA()
    planner = PlanExecutor(
        _plan_llm(
            {
                "steps": [
                    {"id": "a", "action": "a", "params": {}, "depends_on": []},
                    {"id": "b", "action": "b", "params": {}, "depends_on": []},
                ]
            }
        ),
        executor,
        system="sys",
        messages=[{"role": "user", "content": "do"}],
        event_log=events,
        checkpoint_store=checkpoints,
    )

    streamed: list = []
    async for event in planner.stream("do", run_id="R-handoff"):
        streamed.append(event)

    run = _final_run(streamed)
    assert run.pending_handoff is not None
    assert run.pending_handoff.peer_name == "peer_x"
    tool_msgs = [m for m in run.messages if m.get("role") == "tool"]
    assert all("b" not in m.get("content", "") for m in tool_msgs), (
        "sibling 'b' committed into the transcript after the handoff parked"
    )


class _SlowFirst:
    async def __call__(self, call, *, run_id: str = "") -> ToolResult:
        if call.name == "a":
            await asyncio.sleep(0.05)  # completes LAST
        return ToolResult(ToolOutcome.OK, value={"action": call.name}, tool=call.name)


@pytest.mark.asyncio
async def test_transcript_order_matches_step_order_not_completion_order(tmp_path):
    """Parallel steps' tool messages land in plan order, not racy completion
    order (a is slow, b is fast — but 'a' must still precede 'b')."""
    events, checkpoints = _stores(tmp_path)
    planner = PlanExecutor(
        _plan_llm(
            {
                "steps": [
                    {"id": "a", "action": "a", "params": {}, "depends_on": []},
                    {"id": "b", "action": "b", "params": {}, "depends_on": []},
                ]
            }
        ),
        _SlowFirst(),
        system="sys",
        messages=[{"role": "user", "content": "do"}],
        event_log=events,
        checkpoint_store=checkpoints,
    )

    streamed: list = []
    async for event in planner.stream("do", run_id="R-order"):
        streamed.append(event)

    run = _final_run(streamed)
    tool_msgs = [m for m in run.messages if m.get("role") == "tool"]
    contents = [m.get("content", "") for m in tool_msgs]
    a_idx = next(i for i, c in enumerate(contents) if '"a"' in c)
    b_idx = next(i for i, c in enumerate(contents) if '"b"' in c)
    assert a_idx < b_idx, f"transcript out of step order: {contents}"
