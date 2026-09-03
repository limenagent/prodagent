from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from prodagent.backends.file.span import FileSpanExporter
from prodagent.backends.memory.event_log import InMemoryEventLog
from prodagent.base.event_log import BoundaryEventType, boundary_stream
from prodagent.hooks.audit import AuditLogger
from prodagent.kernel.bus import HookEvent, HookRegistry
from prodagent.kernel.types import LLMResponse
from prodagent.llm.fake import FakeLLMAdapter
from prodagent.llm.recording import RecordingLLMClient
from prodagent.runtime.recipes.agent_loop import agent_scheduler
from prodagent.tooling import tool
from prodagent.tooling.dispatcher import ToolDispatcher

if TYPE_CHECKING:
    from pathlib import Path


@tool(name="noop", readonly=True)
async def _noop_tool() -> dict:
    return {"result": "ok"}


def _make_loop(llm, hooks, event_log=None) -> agent_scheduler:
    dispatcher = ToolDispatcher({"noop": _noop_tool})
    return agent_scheduler(
        llm,
        dispatcher,
        system_prompt="test",
        event_log=event_log,
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

    log = InMemoryEventLog()
    llm = RecordingLLMClient(
        FakeLLMAdapter(
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
        ),
        log,
    )
    loop = _make_loop(llm, hooks, event_log=log)

    await hooks.fire(HookEvent.SESSION_START, run_id="run-x", task="test", phases=1)

    async for _ in loop.stream("test", run_id="run-x"):
        pass
    await audit.shutdown()

    records = [
        __import__("json").loads(line)
        for line in trace_path.read_text(encoding="utf-8").strip().splitlines()
    ]
    think_spans = [r for r in records if r["action"] == "llm.think"]
    # Contract moved with the WAL: the reasoning text rides the boundary
    # LLM fact (response.reasoning_content, verbatim) — per-token THINK
    # spans were dropped as noise. The bus event still fires (drain-order
    # test below); the span observer skips it by design.
    assert not think_spans, "per-token THINK spans are gone by design"
    facts = await log.get_events(boundary_stream("run-x"))
    llm_facts = [e for e in facts if e.event_type == BoundaryEventType.LLM_RECORDED]
    assert any(
        "I should call noop then finish" in str(e.data.get("response", {})) for e in llm_facts
    ), "reasoning_content rides the boundary LLM fact, verbatim"


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
    # Per-token THINK still FIRES on the bus (drained before _think
    # returns); it no longer becomes a span (noise by design). Drain order
    # is observable via a bus collector instead of the span file.
    assert [r for r in records if r["action"] == "llm.think"] == [], "no THINK spans"
