from __future__ import annotations

import pytest

from prodagent.core.types import ToolMeta
from prodagent.guardrail.permission import ContextTaintMonitor, TaintLevel
from prodagent.hooks.bundles.security import PermissionHooks
from prodagent.hooks.registry import HookEvent, HookRegistry
from prodagent.tooling.base import FunctionTool
from prodagent.tooling.registry import ToolRegistry


async def _noop_async(**kwargs):
    return {}


def _registry_with(name: str, meta: ToolMeta) -> ToolRegistry:
    reg = ToolRegistry()
    reg.register(
        FunctionTool(
            name=name,
            fn=_noop_async,
            meta=meta,
            schema={"name": name, "input_schema": {"type": "object"}},
        )
    )
    return reg


async def test_taint_resets_on_session_start():
    monitor = ContextTaintMonitor()
    monitor.taint = TaintLevel.SENSITIVE

    hooks = HookRegistry()
    PermissionHooks(taint_monitor=monitor).attach(hooks)

    await hooks.fire(HookEvent.SESSION_START, run_id="new-run", task="fresh task")
    assert monitor.taint == TaintLevel.PUBLIC


async def test_taint_does_not_leak_between_runs():
    registry = _registry_with("query_user", ToolMeta(name="query_user", produces_pii=True))
    monitor = ContextTaintMonitor(tool_registry=registry)
    hooks = HookRegistry()
    PermissionHooks(
        tool_registry=registry,
        taint_monitor=monitor,
    ).attach(hooks)

    await hooks.fire(HookEvent.SESSION_START, run_id="r1", task="task-1")
    assert monitor.taint == TaintLevel.PUBLIC
    from prodagent.hooks.checkpoint import CheckPoint

    await hooks.check_blocking(CheckPoint.TOOL_RESULT, name="query_user", result={"name": "Alice"})
    assert monitor.taint == TaintLevel.RESTRICTED

    await hooks.fire(HookEvent.SESSION_END, run_id="r1")

    await hooks.fire(HookEvent.SESSION_START, run_id="r2", task="task-2")
    assert monitor.taint == TaintLevel.PUBLIC


def test_begin_session_twice_raises():
    monitor = ContextTaintMonitor()
    monitor.begin_session()
    with pytest.raises(RuntimeError, match="session already active"):
        monitor.begin_session()


def test_end_session_allows_new_begin_session():
    monitor = ContextTaintMonitor()
    monitor.begin_session()
    monitor.taint = TaintLevel.SENSITIVE
    monitor.end_session()
    monitor.begin_session()
    assert monitor.taint == TaintLevel.PUBLIC


async def test_session_start_fires_without_session_end_logs_error(caplog):
    import logging

    monitor = ContextTaintMonitor()
    hooks = HookRegistry()
    PermissionHooks(taint_monitor=monitor).attach(hooks)

    await hooks.fire(HookEvent.SESSION_START, run_id="r1", task="task-1")
    monitor.taint = TaintLevel.SENSITIVE

    with caplog.at_level(logging.ERROR, logger="prodagent.hooks.registry"):
        await hooks.fire(HookEvent.SESSION_START, run_id="r2", task="task-2")

    assert any(
        "session already active" in rec.message or "session already active" in str(rec.exc_info)
        for rec in caplog.records
        if rec.exc_info
    )
    assert monitor.taint == TaintLevel.SENSITIVE
