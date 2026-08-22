from __future__ import annotations

import asyncio
import contextlib

from prodagent import Agent, AgentConfig, ExecutionMode, RunState, SideEffectLevel, ToolMeta
from prodagent.backends.file.checkpoint import FileCheckpointStore
from prodagent.core.exceptions import SuspendPendingApproval
from prodagent.core.types import ToolCall
from prodagent.hooks.approval import (
    ApprovalDecision,
    ApprovalGate,
    ContextAwareApprovalFormatter,
)
from prodagent.hooks.bundles.security import ApprovalHooks
from prodagent.hooks.registry import HookRegistry
from prodagent.llm.fake import script
from prodagent.tooling import tool


def test_approval_decision_enum_is_minimal():
    """The decision surface is exactly approve/reject — nothing auto-executes."""
    assert {d.value for d in ApprovalDecision} == {"approve", "reject"}


class TestFormatterIntegration:
    def test_formatter_called_on_human_review(self):
        """Gate suspends with a formatted ApprovalRequest in _pending."""
        gate = ApprovalGate()
        call = ToolCall(
            name="drop_table", params={"table": "orders", "count": 5, "environment": "production"}
        )

        async def _evaluate() -> None:
            with contextlib.suppress(SuspendPendingApproval):
                await gate.evaluate(call, run_id="r-fmt")

        asyncio.run(_evaluate())

        assert gate._pending, "gate did not register a pending request"
        req = next(iter(gate._pending.values()))
        assert req.context_summary, "formatter did not produce a message"
        assert "drop_table" in req.context_summary
        assert "Reversible" not in req.context_summary
        assert "PRODUCTION" in req.context_summary

    def test_formatter_marks_approval_required_header(self):
        fmt = ContextAwareApprovalFormatter()
        call = ToolCall(name="drop_table", params={})
        msg = fmt.format(call)
        assert "[APPROVAL REQUIRED]" in msg
        assert "Reversible" not in msg

    def test_formatter_never_dumps_raw_json_over_200_chars(self):
        fmt = ContextAwareApprovalFormatter()
        big_params = {"data": "x" * 500}
        call = ToolCall(name="x", params=big_params)
        msg = fmt.format(call)
        assert "..." in msg


@tool(
    name="restart_pod",
    meta=ToolMeta(name="restart_pod", side_effect_level=SideEffectLevel.HIGH),
)
async def restart_pod(service: str) -> dict:
    return {"restarted": service}


def _production_fw():
    from prodagent.core.config import FrameworkConfig, production

    return production(FrameworkConfig.default())


def _high_tool_agent(llm, hitl: ApprovalHooks, *, store=None) -> Agent:
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
            extensions=[hitl],
            framework=_production_fw(),
        ),
    )


class TestRejectSoftVeto:
    def test_reject_is_soft_veto_run_continues(self, tmp_path):
        """Pre-submit REJECT before resume: run completes with the blocked tool recorded."""
        store = FileCheckpointStore(tmp_path)
        gate = ApprovalGate()
        hitl = ApprovalHooks(gate=gate)
        llm1 = script({"tool": "restart_pod", "params": {"service": "api"}})
        agent1 = _high_tool_agent(llm1, hitl, store=store)
        run1 = asyncio.run(agent1.chat("restart api", session_id="run-soft-veto"))
        assert run1.state == RunState.SUSPENDED

        asyncio.run(gate.submit_decision(run1.pending_approval_id, ApprovalDecision.REJECT))

        llm2 = script({"content": "Could not restart; approval denied."})
        agent2 = _high_tool_agent(llm2, hitl, store=store)
        run2 = asyncio.run(agent2.chat(resume=True, session_id="run-soft-veto"))
        assert run2.state == RunState.COMPLETED
