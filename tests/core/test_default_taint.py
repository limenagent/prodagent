"""Default-path taint monitoring — wired by attach_default_hooks, no PermissionMatrix."""

from __future__ import annotations

import pytest

from prodagent.core.types import SideEffectLevel, ToolMeta
from prodagent.guardrail.permission import TaintLevel
from prodagent.hooks.bundles.security import PermissionHooks
from prodagent.hooks.checkpoint import CheckPoint
from prodagent.hooks.registry import HookEvent, HookRegistry
from prodagent.runtime.agent import Agent
from prodagent.tooling.base import FunctionTool
from prodagent.tooling.registry import ToolRegistry


def _registry_with_exfil_tool() -> ToolRegistry:
    reg = ToolRegistry()
    reg.register(
        FunctionTool(
            name="send_email",
            fn=_noop,
            meta=ToolMeta(
                name="send_email",
                is_readonly=False,
                side_effect_level=SideEffectLevel.MEDIUM,
                is_exfiltration_tool=True,
            ),
            schema={"name": "send_email", "input_schema": {"type": "object"}},
        )
    )
    return reg


async def _noop(**kwargs):
    return {}


async def test_default_hooks_wire_taint_monitor():
    agent = Agent("t", tool_registry=_registry_with_exfil_tool())
    hooks = agent.attach_default_hooks()
    assert hooks is not None
    # SESSION_START must trigger begin_session on the default monitor.
    await hooks.fire(HookEvent.SESSION_START, run_id="r1", task="task")
    # After a tool result containing PII, taint must escalate to RESTRICTED.
    await hooks.check_blocking(
        CheckPoint.TOOL_RESULT, name="query_user", result={"email": "alice@example.com"}
    )
    # find the wired PermissionHooks and inspect its monitor
    monitor = _find_default_taint_monitor(hooks)
    assert monitor is not None
    assert monitor.taint in (TaintLevel.RESTRICTED, TaintLevel.SENSITIVE)


async def test_default_taint_blocks_exfiltration_after_pii():
    agent = Agent("t", tool_registry=_registry_with_exfil_tool())
    hooks = agent.attach_default_hooks()
    await hooks.fire(HookEvent.SESSION_START, run_id="r1", task="task")
    await hooks.check_blocking(
        CheckPoint.TOOL_RESULT, name="query_user", result={"email": "alice@example.com"}
    )
    from prodagent.core.exceptions import DataFlowBlocked

    with pytest.raises(DataFlowBlocked, match="send_email"):
        await hooks.check_blocking(CheckPoint.TOOL_CALL, name="send_email")


def test_user_permission_hooks_suppresses_default_taint():
    agent = Agent("t", tool_registry=_registry_with_exfil_tool()).extend(
        PermissionHooks(taint_monitor=None)
    )
    agent.attach_default_hooks()
    monitor = _find_default_taint_monitor(agent.config.hooks)
    assert monitor is None


def _find_default_taint_monitor(hooks: HookRegistry):
    for handlers in hooks._check_handlers.values():
        for _priority, h in handlers:
            wrapped = getattr(h, "__wrapped__", None)
            instance = (
                getattr(wrapped, "__self__", None) if wrapped else getattr(h, "__self__", None)
            )
            monitor = getattr(instance, "_monitor", None)
            if monitor is not None and hasattr(monitor, "taint"):
                return monitor
    return None
