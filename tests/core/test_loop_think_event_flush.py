from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from prodagent.backends.file.span import FileSpanExporter
from prodagent.core.types import LLMResponse
from prodagent.hooks.events import HookEvent
from prodagent.hooks.registry import HookRegistry
from prodagent.llm.fake import FakeLLMAdapter
from prodagent.resilience.observability.audit import AuditLogger
from prodagent.runtime.reactive import ReactiveLoop
from prodagent.tooling import tool
from prodagent.tooling.dispatcher import ToolDispatcher

if TYPE_CHECKING:
    from pathlib import Path


@tool(name="noop", readonly=True)
async def _noop_tool() -> dict:
    return {"result": "ok"}


def _make_loop(llm: FakeLLMAdapter, hooks: HookRegistry) -> ReactiveLoop:
    dispatcher = ToolDispatcher({"noop": _noop_tool})
    return ReactiveLoop(
        llm,
        dispatcher,
        system_prompt="test",
        tools_schema=[],
        hooks=hooks,
    )


@pytest.mark.asyncio
async def test_reasoning_content_think_event_lands_in_audit_trace(tmp_path: Path):
    trace_path = tmp_path / "trace.jsonl"
    audit = AuditLogger(exporter=FileSpanExporter(trace_path))
    hooks = HookRegistry()

    from prodagent.hooks.bundles.observability import SpanObserverHooks

    SpanObserverHooks(audit=audit).attach(hooks)

    llm = FakeLLMAdapter(
        responses=[
            LLMResponse(
                content="done",
                reasoning_content="I should call noop then finish",
                tool_calls=[],
                stop_reason="end_turn",
                input_tokens=10,
                output_tokens=5,
            ),
        ]
    )
    loop = _make_loop(llm, hooks)

    await hooks.fire(HookEvent.SESSION_START, run_id="run-x", task="test", phases=1)

    async for _ in loop.stream("test", run_id="run-x"):
        pass
    await audit.shutdown()

    records = [
        __import__("json").loads(line)
        for line in trace_path.read_text(encoding="utf-8").strip().splitlines()
    ]
    think_spans = [r for r in records if r["action"] == "llm.think"]
    assert think_spans, "THINK event must fire for reasoning_content"
    assert think_spans[-1]["llm_reasoning"] == "I should call noop then finish", (
        "reasoning_content must land in the span before _think returns"
    )


@pytest.mark.asyncio
async def test_per_token_think_fires_drained_before_think_returns(tmp_path: Path):
    trace_path = tmp_path / "trace.jsonl"
    audit = AuditLogger(exporter=FileSpanExporter(trace_path))
    hooks = HookRegistry()

    from prodagent.hooks.bundles.observability import SpanObserverHooks

    SpanObserverHooks(audit=audit).attach(hooks)

    llm = FakeLLMAdapter(
        responses=[
            LLMResponse(
                content="alpha beta gamma delta",
                stop_reason="end_turn",
                input_tokens=10,
                output_tokens=5,
            ),
        ]
    )
    loop = _make_loop(llm, hooks)

    await hooks.fire(HookEvent.SESSION_START, run_id="run-y", task="test", phases=1)
    async for _ in loop.stream("test", run_id="run-y"):
        pass
    await audit.shutdown()

    records = [
        __import__("json").loads(line)
        for line in trace_path.read_text(encoding="utf-8").strip().splitlines()
    ]
    think_spans = [r for r in records if r["action"] == "llm.think"]
    assert think_spans, "per-token THINK events must fire"
    assert "delta" in think_spans[-1]["llm_reasoning"], (
        "last per-token THINK fire must land in the span before _think returns; "
        f"got llm_reasoning={think_spans[-1]['llm_reasoning']!r}"
    )
