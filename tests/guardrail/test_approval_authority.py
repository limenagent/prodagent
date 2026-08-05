from __future__ import annotations

import asyncio
import contextlib

from prodagent import Agent, ExecutionMode, RunState, SideEffectLevel, ToolMeta
from prodagent.backends.file.checkpoint import FileCheckpointStore
from prodagent.core.exceptions import SuspendPendingApproval
from prodagent.core.types import ToolCall
from prodagent.guardrail.approval import (
    ApprovalDecision,
    ApprovalGate,
    ContextAwareApprovalFormatter,
    extract_confidence,
    should_request_review,
)
from prodagent.hooks.bundles.security import ApprovalHooks
from prodagent.hooks.registry import HookRegistry
from prodagent.llm.fake import script
from prodagent.tooling import tool


class TestMatrixRouting:
    def test_high_confidence_high_reversibility_auto_executes(self):
        meta = ToolMeta(name="read_config", reversibility=0.95)
        assert should_request_review(meta, 0.95) == ApprovalDecision.AUTO_EXECUTE

    def test_high_confidence_low_reversibility_brief_approval(self):
        meta = ToolMeta(name="update_config", reversibility=0.2)
        assert should_request_review(meta, 0.95) == ApprovalDecision.BRIEF_APPROVAL

    def test_low_confidence_high_reversibility_auto_executes_with_log(self):
        meta = ToolMeta(name="restart_test_env", reversibility=0.9)
        assert should_request_review(meta, 0.3) == ApprovalDecision.AUTO_EXECUTE

    def test_low_confidence_low_reversibility_full_approval(self):
        meta = ToolMeta(name="drop_table", reversibility=0.1)
        assert should_request_review(meta, 0.3) == ApprovalDecision.FULL_APPROVAL

    def test_reversibility_read_from_meta_not_hardcoded(self):
        meta_rev_high = ToolMeta(name="x", reversibility=0.95)
        meta_rev_low = ToolMeta(name="x", reversibility=0.1)
        assert should_request_review(meta_rev_high, 0.9) == ApprovalDecision.AUTO_EXECUTE
        assert should_request_review(meta_rev_low, 0.9) == ApprovalDecision.BRIEF_APPROVAL


class TestFormatterIntegration:
    def test_formatter_called_on_human_review(self):
        """Gate suspends with a formatted ApprovalRequest in _pending."""
        gate = ApprovalGate()
        call = ToolCall(
            name="drop_table", params={"table": "orders", "count": 5, "environment": "production"}
        )

        async def _evaluate() -> None:
            with contextlib.suppress(SuspendPendingApproval):
                await gate.evaluate(call, confidence=0.3, reversibility=0.1, run_id="r-fmt")

        asyncio.run(_evaluate())

        assert gate._pending, "gate did not register a pending request"
        req = next(iter(gate._pending.values()))
        assert req.context_summary, "formatter did not produce a message"
        assert "drop_table" in req.context_summary
        assert "Reversible : NO" in req.context_summary
        assert "PRODUCTION" in req.context_summary

    def test_formatter_flags_irreversible(self):
        fmt = ContextAwareApprovalFormatter()
        call = ToolCall(name="drop_table", params={})
        msg = fmt.format(call, reversibility=0.1)
        assert "Reversible : NO" in msg

    def test_formatter_flags_reversible(self):
        fmt = ContextAwareApprovalFormatter()
        call = ToolCall(name="read_config", params={})
        msg = fmt.format(call, reversibility=0.95)
        assert "Reversible : YES" in msg

    def test_formatter_never_dumps_raw_json_over_200_chars(self):
        fmt = ContextAwareApprovalFormatter()
        big_params = {"data": "x" * 500}
        call = ToolCall(name="x", params=big_params)
        msg = fmt.format(call, reversibility=0.5)
        assert "..." in msg


@tool(
    name="restart_pod",
    meta=ToolMeta(name="restart_pod", side_effect_level=SideEffectLevel.HIGH, reversibility=0.2),
)
async def restart_pod(service: str) -> dict:
    return {"restarted": service}


def _high_tool_agent(llm, hitl: ApprovalHooks, *, store=None) -> Agent:
    return Agent(
        name="ops",
        system_prompt="Restart the pod.",
        tools=[restart_pod],
        llm=llm,
        hooks=HookRegistry(),
        checkpoint=store,
        mode=ExecutionMode.REACTIVE,
        extensions=[hitl],
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


class TestConfidenceSource:
    def test_reads_confidence_from_metadata(self):
        call = ToolCall(name="x", params={}, metadata={"confidence": 0.8})
        assert extract_confidence(call) == 0.8

    def test_returns_none_when_not_reported(self):
        call = ToolCall(name="x", params={})
        assert extract_confidence(call) is None

    def test_clamps_out_of_range(self):
        assert (
            extract_confidence(ToolCall(name="x", params={}, metadata={"confidence": 1.5})) == 1.0
        )
        assert (
            extract_confidence(ToolCall(name="x", params={}, metadata={"confidence": -0.3})) == 0.0
        )

    def test_falls_back_on_garbage(self):
        call = ToolCall(name="x", params={}, metadata={"confidence": "not a number"})
        assert extract_confidence(call) is None

    def test_default_confidence_routes_low_reversibility_to_full_approval(self):
        meta = ToolMeta(name="drop_table", reversibility=0.1)
        assert (
            should_request_review(meta, extract_confidence(ToolCall(name="drop_table", params={})))
            == ApprovalDecision.FULL_APPROVAL
        )
