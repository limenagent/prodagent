from __future__ import annotations

import logging

import pytest

from prodagent import DataFlowBlocked
from prodagent.core.types import ToolMeta
from prodagent.guardrail.permission import ContextTaintMonitor, TaintLevel
from prodagent.hooks.bundles.security import PermissionHooks
from prodagent.hooks.checkpoint import CheckPoint
from prodagent.hooks.registry import HookRegistry
from prodagent.tooling.base import FunctionTool
from prodagent.tooling.registry import ToolRegistry


async def _noop_async(**kwargs):
    return {}


def _registry_with(*tools: tuple[str, ToolMeta]) -> ToolRegistry:
    reg = ToolRegistry()
    for name, meta in tools:
        reg.register(
            FunctionTool(
                name=name,
                fn=_noop_async,
                meta=meta,
                schema={"name": name, "input_schema": {"type": "object"}},
            )
        )
    return reg


class _FailingBreaker:
    def __init__(self) -> None:
        self.record_calls = 0

    def check(self, agent_id: str) -> None:
        pass

    def record_violation(self, agent_id: str, reason: str = "") -> None:
        self.record_calls += 1
        raise RuntimeError("ViolationStore backend is down")


async def test_breaker_failure_is_logged_not_swallowed(caplog):
    monitor = ContextTaintMonitor()
    monitor.taint = TaintLevel.RESTRICTED
    failing_breaker = _FailingBreaker()

    registry = _registry_with(
        ("send_email", ToolMeta(name="send_email", is_exfiltration_tool=True)),
    )

    hooks = HookRegistry()
    PermissionHooks(
        tool_registry=registry,
        taint_monitor=monitor,
        circuit_breaker=failing_breaker,
        agent_id="agent-x",
    ).attach(hooks)

    with (
        caplog.at_level(logging.ERROR, logger="prodagent.hooks.bundles.security"),
        pytest.raises(DataFlowBlocked),
    ):
        await hooks.check_blocking(CheckPoint.TOOL_CALL, name="send_email", params={})

    assert any(
        "Circuit breaker failed to record violation" in rec.message for rec in caplog.records
    ), f"Expected breaker failure log; got: {[r.message for r in caplog.records]}"
    assert failing_breaker.record_calls == 1


async def test_breaker_success_path_still_works():
    monitor = ContextTaintMonitor()
    monitor.taint = TaintLevel.SENSITIVE

    class _OkBreaker:
        def __init__(self) -> None:
            self.recorded = []

        def check(self, agent_id: str) -> None:
            pass

        def record_violation(self, agent_id: str, reason: str = "") -> None:
            self.recorded.append((agent_id, reason))

    ok_breaker = _OkBreaker()
    registry = _registry_with(
        ("http_post", ToolMeta(name="http_post", is_exfiltration_tool=True)),
    )
    hooks = HookRegistry()
    PermissionHooks(
        tool_registry=registry,
        taint_monitor=monitor,
        circuit_breaker=ok_breaker,
        agent_id="agent-y",
    ).attach(hooks)

    with pytest.raises(DataFlowBlocked):
        await hooks.check_blocking(CheckPoint.TOOL_CALL, name="http_post", params={})

    assert len(ok_breaker.recorded) == 1
    assert ok_breaker.recorded[0][0] == "agent-y"
