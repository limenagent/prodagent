from __future__ import annotations

import asyncio
import contextlib

from prodagent.core.exceptions import SuspendPendingApproval
from prodagent.guardrail.approval import ApprovalDecision, ApprovalGate
from prodagent.hooks.bundles.security import ApprovalHooks
from prodagent.hooks.events import HookEvent
from prodagent.hooks.registry import HookRegistry


def _make_hooks() -> tuple[HookRegistry, ApprovalHooks, list[dict]]:
    """Wire up ApprovalHooks + capture APPROVAL_REQUEST events."""
    gate = ApprovalGate()
    hitl = ApprovalHooks(gate=gate)
    hooks = HookRegistry()
    hitl.attach(hooks)

    captured: list[dict] = []

    async def _capture(**data):
        captured.append(data)

    hooks.register_event(HookEvent.APPROVAL_REQUEST, _capture)
    return hooks, hitl, captured


def test_approval_request_event_fires_on_fresh_request():
    hooks, hitl, captured = _make_hooks()

    async def _call() -> None:
        with contextlib.suppress(SuspendPendingApproval):
            await hitl.gate_request(
                name="restart_pod",
                params={"service": "api"},
                run_id="r1",
            )

    asyncio.run(_call())

    assert len(captured) == 1, f"expected one APPROVAL_REQUEST event, got {captured}"
    ev = captured[0]
    assert ev["name"] == "restart_pod"
    assert ev["level"] == "HIGH"
    assert ev["run_id"] == "r1"


def test_approval_request_event_not_refired_on_resume():
    """Resume path: pending_approval_id is set, event must NOT fire again."""
    hooks, hitl, captured = _make_hooks()
    gate = hitl._gate

    async def _fresh_then_resume() -> None:
        with contextlib.suppress(SuspendPendingApproval):
            await hitl.gate_request(
                name="restart_pod",
                params={"service": "api"},
                run_id="r1",
            )

    asyncio.run(_fresh_then_resume())
    assert len(captured) == 1

    req_id = next(iter(gate._pending.keys()))
    asyncio.run(gate.submit_decision(req_id, ApprovalDecision.APPROVE))

    async def _resume() -> None:
        with contextlib.suppress(Exception):
            await hitl.gate_request(
                name="restart_pod",
                params={"service": "api"},
                run_id="r1",
                pending_approval_id=req_id,
            )

    asyncio.run(_resume())

    assert len(captured) == 1, "APPROVAL_REQUEST must not re-fire on resume"
